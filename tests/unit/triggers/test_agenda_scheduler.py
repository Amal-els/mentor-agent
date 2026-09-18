import collections
import datetime
import uuid

import pytest
from sqlalchemy import select

from app.agenda.models import Accomplishment, Pair
from app.core.clock import FrozenClock
from app.triggers.agenda import agenda_scheduler
from app.ingest.base import Healthy, Unauthorized
from app.core.models import Goal, User
from app.triggers.agenda.agenda_scheduler import (
    _get_scheduled_report_user_ids,
    _is_just_ended,
    _process_meeting_end,
    poll_meeting_end_once,
    run_notion_goals_sync_once,
    run_notion_member_revocation_once,
    run_notion_pair_sync_once,
    run_notion_user_provisioning_once,
)

NOW = datetime.datetime(2026, 8, 14, 12, 0, 0, tzinfo=datetime.UTC)


# --- Job 1: Notion org-data sync --------------------------------------------


class _FixtureNotionPairClient:
    def __init__(self, edges):
        self._edges = edges

    def fetch(self, window, owner_user_id):
        return list(self._edges)


def _email(label: str) -> str:
    # notion_owner_email is globally unique (uq_users_notion_owner_email)
    # and this suite runs against a shared, non-transactional Postgres
    # instance — suffix every literal email with a fresh uuid so parallel
    # test functions never collide, same convention as
    # test_notion_pair_sync.py's own _email.
    return f"{label}-{uuid.uuid4()}@example.com"


def test_run_notion_pair_sync_once_creates_a_real_pair_row(pg_session, make_user):
    report_email, manager_email = _email("report"), _email("manager")
    report_id = make_user(notion_owner_email=report_email)
    make_user(notion_owner_email=manager_email)

    client = _FixtureNotionPairClient(
        [
            {
                "report_notion_email": report_email,
                "manager_notion_email": manager_email,
            }
        ]
    )

    count = run_notion_pair_sync_once(pg_session, client, clock=FrozenClock(at=NOW))

    assert count == 1
    pair = (
        pg_session.query(Pair).filter_by(report_user_id=report_id, ended_at=None).one()
    )
    assert pair.report_user_id == report_id


def test_run_notion_pair_sync_once_is_idempotent_across_polls(pg_session, make_user):
    report_email, manager_email = _email("idem-report"), _email("idem-manager")
    report_id = make_user(notion_owner_email=report_email)
    make_user(notion_owner_email=manager_email)

    raw = {
        "report_notion_email": report_email,
        "manager_notion_email": manager_email,
    }
    client = _FixtureNotionPairClient([raw])

    run_notion_pair_sync_once(pg_session, client, clock=FrozenClock(at=NOW))
    run_notion_pair_sync_once(pg_session, client, clock=FrozenClock(at=NOW))

    assert pg_session.query(Pair).filter_by(report_user_id=report_id).count() == 1


def test_run_notion_pair_sync_once_returns_zero_when_no_edges(pg_session):
    count = run_notion_pair_sync_once(
        pg_session, _FixtureNotionPairClient([]), clock=FrozenClock(at=NOW)
    )
    assert count == 0


# --- Job 1c/1d: Notion auto-provision/auto-revoke ---------------------------


class _FakeNotionDirectoryClient:
    def __init__(self, directory=None, member_ids=None):
        self._directory = directory or []
        self._member_ids = member_ids

    def fetch_directory(self):
        return list(self._directory)

    def fetch_active_member_ids(self):
        return self._member_ids


def test_run_notion_user_provisioning_once_creates_users_and_emails_them(
    pg_session, monkeypatch
):
    sent = []

    class _FakeEmailDeliverer:
        def send(self, to, subject, body):
            sent.append(to)
            return {"sent": True}

    monkeypatch.setattr(
        "app.triggers.agenda.setup_router.EmailDeliverer", _FakeEmailDeliverer
    )

    person_id = f"notion-person-{uuid.uuid4()}"
    email = f"newperson-{uuid.uuid4()}@example.com"
    client = _FakeNotionDirectoryClient(
        directory=[
            {
                "notion_person_id": person_id,
                "email": email,
                "display_name": "New Person",
            }
        ]
    )

    count = run_notion_user_provisioning_once(
        pg_session, client, FrozenClock(at=NOW), base_url="http://testserver"
    )

    assert count == 1
    assert sent == [email]
    row = (
        pg_session.query(User).filter_by(notion_person_id=person_id).one()
    )
    assert row.setup_token is not None


def test_run_notion_user_provisioning_once_skips_email_without_base_url(
    pg_session, monkeypatch
):
    sent = []

    class _FakeEmailDeliverer:
        def send(self, to, subject, body):
            sent.append(to)
            return {"sent": True}

    monkeypatch.setattr(
        "app.triggers.agenda.setup_router.EmailDeliverer", _FakeEmailDeliverer
    )

    client = _FakeNotionDirectoryClient(
        directory=[
            {
                "notion_person_id": f"notion-person-{uuid.uuid4()}",
                "email": f"noemail-{uuid.uuid4()}@example.com",
                "display_name": None,
            }
        ]
    )

    count = run_notion_user_provisioning_once(
        pg_session, client, FrozenClock(at=NOW), base_url=""
    )

    assert count == 1
    assert sent == []


def test_run_notion_member_revocation_once_clears_departed_users(
    pg_session, make_user
):
    # revoke_departed_notion_users has NO owner-scoping — it scans every
    # linked User row in the WHOLE table, including real accounts, since
    # this suite shares the same live Postgres instance the real app
    # uses (confirmed live: an earlier version of this test, passing a
    # bare member_ids=set(), actually revoked three real linked users'
    # real agenda_client_secret as a side effect of running this test).
    # Snapshotting existing linked ids BEFORE creating the fake
    # "departed" row and including them in the fake member set keeps
    # every real/other-test account untouched.
    existing_ids = set(
        pg_session.execute(
            select(User.notion_person_id).where(User.notion_person_id.is_not(None))
        )
        .scalars()
        .all()
    )

    person_id = f"notion-person-{uuid.uuid4()}"
    uid = make_user(
        notion_person_id=person_id, agenda_client_secret=f"secret-{uuid.uuid4()}"
    )

    client = _FakeNotionDirectoryClient(member_ids=existing_ids)

    count = run_notion_member_revocation_once(pg_session, client, FrozenClock(at=NOW))

    assert count >= 1
    row = pg_session.get(User, uid)
    assert row.agenda_client_secret is None
    assert row.notion_access_revoked_at == NOW


def test_run_notion_member_revocation_once_is_a_noop_when_member_ids_unknown(
    pg_session, make_user
):
    person_id = f"notion-person-{uuid.uuid4()}"
    secret = f"secret-{uuid.uuid4()}"
    uid = make_user(notion_person_id=person_id, agenda_client_secret=secret)

    client = _FakeNotionDirectoryClient(member_ids=None)

    count = run_notion_member_revocation_once(pg_session, client, FrozenClock(at=NOW))

    assert count == 0
    row = pg_session.get(User, uid)
    assert row.agenda_client_secret == secret


# --- Job 2: meeting-end detection ------------------------------------------


class _FakeCalendarClient:
    def __init__(self, events):
        self._events = events

    def fetch(self, window, owner_user_id):
        return list(self._events)


def _factory_for(events):
    return lambda session, report_user_id: _FakeCalendarClient(events)


@pytest.fixture(autouse=True)
def _reset_seen_meeting_ids(monkeypatch):
    monkeypatch.setattr(
        agenda_scheduler, "_seen_meeting_ids", collections.deque(maxlen=2000)
    )


def test_is_just_ended_true_within_window():
    ends_at = (NOW - datetime.timedelta(seconds=30)).isoformat()
    assert _is_just_ended(ends_at, NOW) is True


def test_is_just_ended_false_for_an_event_that_ended_long_ago():
    ends_at = (NOW - datetime.timedelta(hours=5)).isoformat()
    assert _is_just_ended(ends_at, NOW) is False


def test_is_just_ended_false_for_missing_or_future_end():
    assert _is_just_ended(None, NOW) is False
    assert (
        _is_just_ended((NOW + datetime.timedelta(seconds=30)).isoformat(), NOW) is False
    )


def test_poll_meeting_end_once_fires_run_post_meeting_flow_for_a_just_ended_event(
    pg_session, make_pair, monkeypatch
):
    pair = make_pair()
    calls = []
    monkeypatch.setattr(
        "app.triggers.agenda.agenda_scheduler.run_post_meeting_flow",
        lambda meeting_id, pair_scope, owner_scope, clock, transcript_text=None: calls.append(
            (meeting_id, pair_scope.report_user_id, owner_scope.owner_user_id)
        ),
    )
    event = {
        "external_id": "evt-just-ended-1",
        "series_id": "series-just-ended",
        "ends_at": (NOW - datetime.timedelta(seconds=10)).isoformat(),
    }

    poll_meeting_end_once(
        lambda: pg_session,
        [pair.report_user_id],
        clock=FrozenClock(at=NOW),
        calendar_client_factory=_factory_for([event]),
    )

    assert calls == [("evt-just-ended-1", pair.report_user_id, pair.report_user_id)]


def test_poll_meeting_end_once_threads_the_event_description_as_transcript_text(
    pg_session, make_pair, monkeypatch
):
    """Real-world finding: without this, a real Calendar-triggered
    meeting_end ALWAYS produced zero agenda items — Capture has no real
    transcript source of its own, and its "ask the user" fallback can't
    work in this headless job. The calendar event's own description
    field (whatever the organizer wrote there) is the only real content
    a real trigger has access to today."""
    pair = make_pair()
    captured = {}
    monkeypatch.setattr(
        "app.triggers.agenda.agenda_scheduler.run_post_meeting_flow",
        lambda meeting_id, pair_scope, owner_scope, clock, transcript_text=None: (
            captured.update(transcript_text=transcript_text)
        ),
    )
    event = {
        "external_id": "evt-with-notes-1",
        "series_id": "series-with-notes",
        "ends_at": (NOW - datetime.timedelta(seconds=10)).isoformat(),
        "description": "Discussed Q3 goals. Bob to ship the migration by Friday.",
    }

    poll_meeting_end_once(
        lambda: pg_session,
        [pair.report_user_id],
        clock=FrozenClock(at=NOW),
        calendar_client_factory=_factory_for([event]),
    )

    assert (
        captured["transcript_text"]
        == "Discussed Q3 goals. Bob to ship the migration by Friday."
    )


class _FakeFathomClient:
    def __init__(self, healthy: bool, transcript: str | None):
        self._healthy = healthy
        self._transcript = transcript
        self.find_transcript_calls = []

    def health(self):
        return Healthy() if self._healthy else Unauthorized()

    def find_transcript(self, window, organizer_email=None):
        self.find_transcript_calls.append((window, organizer_email))
        return self._transcript


def test_process_meeting_end_prefers_fathom_transcript_over_description(
    pg_session, make_pair, monkeypatch
):
    pair = make_pair()
    captured = {}
    monkeypatch.setattr(
        "app.triggers.agenda.agenda_scheduler.run_post_meeting_flow",
        lambda meeting_id, pair_scope, owner_scope, clock, transcript_text=None: (
            captured.update(transcript_text=transcript_text)
        ),
    )
    fake_client = _FakeFathomClient(healthy=True, transcript="Amal: real transcript.")
    event = {
        "external_id": "evt-fathom-1",
        "starts_at": (NOW - datetime.timedelta(minutes=30)).isoformat(),
        "ends_at": (NOW - datetime.timedelta(seconds=10)).isoformat(),
        "actor_reference_key": "calendar:amal@example.com",
        "description": "stale calendar notes",
    }

    succeeded = _process_meeting_end(
        lambda: pg_session,
        pair.report_user_id,
        event,
        FrozenClock(at=NOW),
        fathom_client_factory=lambda: fake_client,
        trust_event_content=True,
    )

    assert succeeded is True
    assert captured["transcript_text"] == "Amal: real transcript."
    assert fake_client.find_transcript_calls[0][1] == "amal@example.com"


def test_process_meeting_end_falls_back_to_description_when_fathom_finds_nothing(
    pg_session, make_pair, monkeypatch
):
    pair = make_pair()
    captured = {}
    monkeypatch.setattr(
        "app.triggers.agenda.agenda_scheduler.run_post_meeting_flow",
        lambda meeting_id, pair_scope, owner_scope, clock, transcript_text=None: (
            captured.update(transcript_text=transcript_text)
        ),
    )
    fake_client = _FakeFathomClient(healthy=True, transcript=None)
    event = {
        "external_id": "evt-fathom-2",
        "starts_at": (NOW - datetime.timedelta(minutes=30)).isoformat(),
        "ends_at": (NOW - datetime.timedelta(seconds=10)).isoformat(),
        "description": "fallback notes",
    }

    _process_meeting_end(
        lambda: pg_session,
        pair.report_user_id,
        event,
        FrozenClock(at=NOW),
        fathom_client_factory=lambda: fake_client,
        trust_event_content=True,
    )

    assert captured["transcript_text"] == "fallback notes"


def test_process_meeting_end_skips_fathom_entirely_when_content_is_untrusted(
    pg_session, make_pair, monkeypatch
):
    """Same cross-pair-leak gate poll_meeting_end_once already applies to
    description must also cover Fathom — a shared-account calendar event
    matched against Fathom could just as easily misattribute one report's
    real recording to another."""
    pair = make_pair()
    captured = {}
    monkeypatch.setattr(
        "app.triggers.agenda.agenda_scheduler.run_post_meeting_flow",
        lambda meeting_id, pair_scope, owner_scope, clock, transcript_text=None: (
            captured.update(transcript_text=transcript_text)
        ),
    )
    fake_client = _FakeFathomClient(healthy=True, transcript="should never be used")
    event = {
        "external_id": "evt-fathom-3",
        "starts_at": (NOW - datetime.timedelta(minutes=30)).isoformat(),
        "ends_at": (NOW - datetime.timedelta(seconds=10)).isoformat(),
        "description": None,
    }

    _process_meeting_end(
        lambda: pg_session,
        pair.report_user_id,
        event,
        FrozenClock(at=NOW),
        fathom_client_factory=lambda: fake_client,
        trust_event_content=False,
    )

    assert captured["transcript_text"] is None
    assert fake_client.find_transcript_calls == []


def test_poll_meeting_end_once_passes_none_transcript_text_when_no_description(
    pg_session, make_pair, monkeypatch
):
    pair = make_pair()
    captured = {}
    monkeypatch.setattr(
        "app.triggers.agenda.agenda_scheduler.run_post_meeting_flow",
        lambda meeting_id, pair_scope, owner_scope, clock, transcript_text=None: (
            captured.update(transcript_text=transcript_text)
        ),
    )
    event = {
        "external_id": "evt-no-notes-1",
        "series_id": "series-no-notes",
        "ends_at": (NOW - datetime.timedelta(seconds=10)).isoformat(),
    }

    poll_meeting_end_once(
        lambda: pg_session,
        [pair.report_user_id],
        clock=FrozenClock(at=NOW),
        calendar_client_factory=_factory_for([event]),
    )

    assert captured["transcript_text"] is None


def test_poll_meeting_end_once_does_not_reprocess_the_same_event_on_a_second_poll(
    pg_session, make_pair, monkeypatch
):
    pair = make_pair()
    calls = []
    monkeypatch.setattr(
        "app.triggers.agenda.agenda_scheduler.run_post_meeting_flow",
        lambda meeting_id, pair_scope, owner_scope, clock, transcript_text=None: calls.append(
            meeting_id
        ),
    )
    event = {
        "external_id": "evt-dedup-1",
        "series_id": "series-dedup",
        "ends_at": (NOW - datetime.timedelta(seconds=10)).isoformat(),
    }

    poll_meeting_end_once(
        lambda: pg_session,
        [pair.report_user_id],
        clock=FrozenClock(at=NOW),
        calendar_client_factory=_factory_for([event]),
    )
    poll_meeting_end_once(
        lambda: pg_session,
        [pair.report_user_id],
        clock=FrozenClock(at=NOW),
        calendar_client_factory=_factory_for([event]),
    )

    assert calls == ["evt-dedup-1"]


def test_poll_meeting_end_once_fires_once_per_report_user_id_for_the_shared_event(
    pg_session, make_pair, monkeypatch
):
    # LiveCalendarClient.fetch ignores owner_user_id entirely, so every
    # whitelisted report_user_id sees the identical event list (same
    # external_id) on every poll — dedup must be per (report_user_id,
    # external_id), not global, or the second report_user_id would never
    # get a meeting_end trigger for this event. See the
    # _seen_meeting_ids comment in app/triggers/agenda_scheduler.py.
    pair_a = make_pair()
    pair_b = make_pair()
    calls = []
    monkeypatch.setattr(
        "app.triggers.agenda.agenda_scheduler.run_post_meeting_flow",
        lambda meeting_id, pair_scope, owner_scope, clock, transcript_text=None: calls.append(
            (meeting_id, pair_scope.report_user_id)
        ),
    )
    event = {
        "external_id": "evt-shared-1",
        "series_id": "series-shared",
        "ends_at": (NOW - datetime.timedelta(seconds=10)).isoformat(),
    }

    poll_meeting_end_once(
        lambda: pg_session,
        [pair_a.report_user_id, pair_b.report_user_id],
        clock=FrozenClock(at=NOW),
        calendar_client_factory=_factory_for([event]),
    )

    assert sorted(calls) == sorted(
        [
            ("evt-shared-1", pair_a.report_user_id),
            ("evt-shared-1", pair_b.report_user_id),
        ]
    )


def test_poll_meeting_end_once_retries_after_a_failed_run(
    pg_session, make_pair, monkeypatch
):
    pair = make_pair()
    call_count = {"n": 0}

    def _flaky_run_post_meeting_flow(
        meeting_id, pair_scope, owner_scope, clock, transcript_text=None
    ):
        call_count["n"] += 1
        if call_count["n"] == 1:
            raise RuntimeError("simulated LLM/DB failure")

    monkeypatch.setattr(
        "app.triggers.agenda.agenda_scheduler.run_post_meeting_flow",
        _flaky_run_post_meeting_flow,
    )
    event = {
        "external_id": "evt-flaky-1",
        "series_id": "series-flaky",
        "ends_at": (NOW - datetime.timedelta(seconds=10)).isoformat(),
    }

    # First poll: run_post_meeting_flow raises -> must not be marked seen.
    poll_meeting_end_once(
        lambda: pg_session,
        [pair.report_user_id],
        clock=FrozenClock(at=NOW),
        calendar_client_factory=_factory_for([event]),
    )
    assert call_count["n"] == 1

    # Second poll: same event, exception no longer occurs -> must be
    # retried (processed), not silently skipped as "already seen".
    poll_meeting_end_once(
        lambda: pg_session,
        [pair.report_user_id],
        clock=FrozenClock(at=NOW),
        calendar_client_factory=_factory_for([event]),
    )
    assert call_count["n"] == 2


def test_poll_meeting_end_once_skips_an_event_that_ended_long_ago(
    pg_session, make_pair, monkeypatch
):
    pair = make_pair()
    calls = []
    monkeypatch.setattr(
        "app.triggers.agenda.agenda_scheduler.run_post_meeting_flow",
        lambda meeting_id, pair_scope, owner_scope, clock, transcript_text=None: calls.append(
            meeting_id
        ),
    )
    event = {
        "external_id": "evt-stale-1",
        "series_id": "series-stale",
        "ends_at": (NOW - datetime.timedelta(hours=5)).isoformat(),
    }

    poll_meeting_end_once(
        lambda: pg_session,
        [pair.report_user_id],
        clock=FrozenClock(at=NOW),
        calendar_client_factory=_factory_for([event]),
    )

    assert calls == []


def test_poll_meeting_end_once_skips_a_non_recurring_event(
    pg_session, make_pair, monkeypatch
):
    """User-requested scope narrowing: only fire meeting_end for a
    recurring calendar event — a real 1-on-1 is almost always a
    repeating series; a one-off meeting (interview, ad hoc call,
    all-hands) never should trigger it."""
    pair = make_pair()
    calls = []
    monkeypatch.setattr(
        "app.triggers.agenda.agenda_scheduler.run_post_meeting_flow",
        lambda meeting_id, pair_scope, owner_scope, clock, transcript_text=None: calls.append(
            meeting_id
        ),
    )
    event = {
        "external_id": "evt-one-off-1",
        "ends_at": (NOW - datetime.timedelta(seconds=10)).isoformat(),
        # no series_id — a one-off meeting
    }

    poll_meeting_end_once(
        lambda: pg_session,
        [pair.report_user_id],
        clock=FrozenClock(at=NOW),
        calendar_client_factory=_factory_for([event]),
    )

    assert calls == []


def test_poll_meeting_end_once_never_leaks_description_across_multiple_report_user_ids(
    pg_session, make_pair, monkeypatch
):
    """Re-review finding: LiveCalendarClient.fetch() ignores owner_user_id
    and always returns the one connected account's shared calendar, so
    with more than one report_user_id whitelisted, every one of them
    sees the IDENTICAL event list on every poll. Forwarding description
    unconditionally would broadcast one person's private meeting notes
    into every OTHER unrelated report/manager pair's synthesized agenda
    item and Slack DM — a real cross-pair privacy leak."""
    pair_a = make_pair()
    pair_b = make_pair()
    captured = []
    monkeypatch.setattr(
        "app.triggers.agenda.agenda_scheduler.run_post_meeting_flow",
        lambda meeting_id, pair_scope, owner_scope, clock, transcript_text=None: (
            captured.append((pair_scope.report_user_id, transcript_text))
        ),
    )
    event = {
        "external_id": "evt-shared-multi-1",
        "series_id": "series-shared-multi",
        "ends_at": (NOW - datetime.timedelta(seconds=10)).isoformat(),
        "description": "private notes about pair_a's 1-on-1",
    }

    poll_meeting_end_once(
        lambda: pg_session,
        [pair_a.report_user_id, pair_b.report_user_id],
        clock=FrozenClock(at=NOW),
        calendar_client_factory=_factory_for([event]),
    )

    assert len(captured) == 2
    for _report_user_id, transcript_text in captured:
        assert transcript_text is None


def test_poll_meeting_end_once_skips_report_user_id_with_no_resolvable_pair(
    pg_session, make_user, monkeypatch
):
    # resolve_pair_scope(session, report_user_id, report_user_id) always
    # resolves for the self-acting case regardless of whether a Pair row
    # exists yet (app/agenda/scope.py) — this scheduler is still expected
    # to bail out cleanly, not raise, on the general None-scope path
    # (e.g. a future acting_user_id != report_user_id caller, or a
    # resolver change), so exercise it directly via a monkeypatched
    # resolve_pair_scope rather than relying on real Pair-row absence,
    # which can't actually produce None here.
    report_user_id = make_user()
    calls = []
    monkeypatch.setattr(
        "app.triggers.agenda.agenda_scheduler.resolve_pair_scope",
        lambda session, report_uid, acting_uid: None,
    )
    monkeypatch.setattr(
        "app.triggers.agenda.agenda_scheduler.run_post_meeting_flow",
        lambda meeting_id, pair_scope, owner_scope, clock, transcript_text=None: calls.append(
            meeting_id
        ),
    )
    event = {
        "external_id": "evt-no-pair-1",
        "series_id": "series-no-pair",
        "ends_at": (NOW - datetime.timedelta(seconds=10)).isoformat(),
    }

    poll_meeting_end_once(
        lambda: pg_session,
        [report_user_id],
        clock=FrozenClock(at=NOW),
        calendar_client_factory=_factory_for([event]),
    )  # must not raise

    assert calls == []


# --- whitelist env var ------------------------------------------------------


def test_get_scheduled_report_user_ids_refuses_when_unset(monkeypatch):
    monkeypatch.delenv("AGENDA_SCHEDULED_REPORT_USER_IDS", raising=False)

    with pytest.raises(RuntimeError, match="AGENDA_SCHEDULED_REPORT_USER_IDS"):
        _get_scheduled_report_user_ids()


def test_get_scheduled_report_user_ids_parses_a_comma_separated_list(monkeypatch):
    monkeypatch.setenv("AGENDA_SCHEDULED_REPORT_USER_IDS", "usr_a, usr_b ,usr_c")

    assert _get_scheduled_report_user_ids() == ["usr_a", "usr_b", "usr_c"]


# --- Job 3: Notion goals/notes sync ------------------------------------------
# REAL BUG FOUND AND FIXED (confirmed live): run_notion_goals_sync_once used
# to eagerly DELETE every Goal row for an owner before refetching — once
# Accomplishment.goal_id (a real FK) started actually being set, this
# crashed with a ForeignKeyViolation and silently took the owner's ENTIRE
# goals/notes sync down, every single 600s cycle, forever. It was also
# already a quieter data-loss bug even without the crash: deleting an
# unchanged Goal before refetching gave it a brand-new id every cycle,
# orphaning any Accomplishment.goal_id pointing at the old one. Zero test
# coverage existed for this function before this fix.

class _FakeGoalsClient:
    source = "notion"

    def __init__(self, rows):
        self._rows = rows

    def fetch(self, window, owner_user_id):
        return list(self._rows)


class _FailingGoalsClient:
    source = "notion"

    def fetch(self, window, owner_user_id):
        raise RuntimeError("Notion API unavailable")


class _EmptyNotesClient:
    source = "notion"

    def fetch(self, window, owner_user_id):
        return []


def _seed_goal(pg_session, owner_user_id, **overrides):
    defaults = dict(
        id=str(uuid.uuid4()),
        owner_user_id=owner_user_id,
        title="Ship the migration",
        status="active",
        created_at=NOW,
        source="notion",
        external_id=f"kr-{uuid.uuid4().hex[:8]}",
        goal_type="key_result",
    )
    defaults.update(overrides)
    goal = Goal(**defaults)
    pg_session.add(goal)
    pg_session.commit()
    return goal


def test_goals_sync_does_not_delete_a_goal_still_referenced_by_an_accomplishment(
    pg_session, make_user, monkeypatch
):
    """The exact regression: pruning must never crash on, or silently
    orphan, a Goal an Accomplishment.goal_id still points to."""
    owner_id = make_user(notion_owner_email=_email("goals-sync-1"))
    goal = _seed_goal(pg_session, owner_id)
    pg_session.add(
        Accomplishment(
            id=str(uuid.uuid4()),
            owner_user_id=owner_id,
            description="Shipped it",
            source_reference_key=f"agenda:{owner_id}",
            occurred_at=NOW,
            goal_id=goal.id,
        )
    )
    pg_session.commit()

    # Notion no longer returns this Key Result at all (e.g. archived) —
    # the exact case the old eager delete "correctly" caught, and the
    # exact case that must NOT delete a still-referenced row.
    monkeypatch.setattr(
        "app.triggers.agenda.agenda_scheduler.LiveNotionGoalsClient",
        lambda notion_owner_email: _FakeGoalsClient([]),
    )
    monkeypatch.setattr(
        "app.triggers.agenda.agenda_scheduler.LiveNotionNotesClient",
        lambda notion_owner_email: _EmptyNotesClient(),
    )

    count = run_notion_goals_sync_once(
        lambda: pg_session, [owner_id], clock=FrozenClock(at=NOW)
    )

    assert count == 1
    # No crash, and the row (and its id) survived.
    assert pg_session.get(Goal, goal.id) is not None


def test_goals_sync_prunes_a_goal_no_longer_returned_and_not_referenced(
    pg_session, make_user, monkeypatch
):
    owner_id = make_user(notion_owner_email=_email("goals-sync-2"))
    goal = _seed_goal(pg_session, owner_id)

    monkeypatch.setattr(
        "app.triggers.agenda.agenda_scheduler.LiveNotionGoalsClient",
        lambda notion_owner_email: _FakeGoalsClient([]),
    )
    monkeypatch.setattr(
        "app.triggers.agenda.agenda_scheduler.LiveNotionNotesClient",
        lambda notion_owner_email: _EmptyNotesClient(),
    )

    run_notion_goals_sync_once(lambda: pg_session, [owner_id], clock=FrozenClock(at=NOW))

    assert pg_session.get(Goal, goal.id) is None


def test_goals_sync_upserts_in_place_instead_of_recreating_with_a_new_id(
    pg_session, make_user, monkeypatch
):
    """The quieter half of the bug: an unchanged Key Result must keep its
    real DB id across a sync (never delete-then-recreate), or any
    Accomplishment.goal_id pointing at it silently goes stale."""
    owner_id = make_user(notion_owner_email=_email("goals-sync-3"))
    goal = _seed_goal(pg_session, owner_id, external_id="kr-stable", current_value=1)

    monkeypatch.setattr(
        "app.triggers.agenda.agenda_scheduler.LiveNotionGoalsClient",
        lambda notion_owner_email: _FakeGoalsClient(
            [
                {
                    "source": "notion",
                    "external_id": "kr-stable",
                    "goal_type": "key_result",
                    "title": "Ship the migration",
                    "current_value": 7,
                    "target_value": 10,
                }
            ]
        ),
    )
    monkeypatch.setattr(
        "app.triggers.agenda.agenda_scheduler.LiveNotionNotesClient",
        lambda notion_owner_email: _EmptyNotesClient(),
    )

    run_notion_goals_sync_once(lambda: pg_session, [owner_id], clock=FrozenClock(at=NOW))

    same_row = pg_session.get(Goal, goal.id)
    assert same_row is not None
    assert same_row.current_value == 7


def test_goals_sync_skips_pruning_entirely_on_a_failed_fetch(
    pg_session, make_user, monkeypatch
):
    """A transient Notion outage must never read as "Notion returned zero
    goals, delete everything" — that would wipe every Goal for this owner
    on every hiccup instead of just skipping the sync this cycle."""
    owner_id = make_user(notion_owner_email=_email("goals-sync-4"))
    goal = _seed_goal(pg_session, owner_id)

    monkeypatch.setattr(
        "app.triggers.agenda.agenda_scheduler.LiveNotionGoalsClient",
        lambda notion_owner_email: _FailingGoalsClient(),
    )
    monkeypatch.setattr(
        "app.triggers.agenda.agenda_scheduler.LiveNotionNotesClient",
        lambda notion_owner_email: _EmptyNotesClient(),
    )

    count = run_notion_goals_sync_once(
        lambda: pg_session, [owner_id], clock=FrozenClock(at=NOW)
    )

    assert count == 1  # the owner still counts as "synced" (notes succeeded)
    assert pg_session.get(Goal, goal.id) is not None
