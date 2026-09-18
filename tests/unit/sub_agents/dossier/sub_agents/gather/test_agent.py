import datetime
import uuid

from sqlalchemy import delete

from app.core.clock import FrozenClock
from app.core.models import Commitment, User, WorkItem
from app.core.scope import OwnerScope
from app.identity.models import Person
from app.identity.types import Resolved, Unattributed
from app.agenda.models import Pair
from app.sub_agents.dossier.sub_agents.gather.agent import (
    ResolvedAttendee,
    _attendee_relationship,
    _attendee_slack_user_ids,
    _blocked_work_items_with_attendees,
    _filter_slack_signals_to_attendees,
    _open_commitments_with_attendees,
    _resolve_pair_scope_for_attendees,
    gather_dossier_context,
)

NOW = datetime.datetime(2026, 8, 19, 9, 0, 0, tzinfo=datetime.UTC)


class _FixtureSlackClient:
    def fetch(self, window, owner_user_id):
        return []


class _FixtureLinearClient:
    def fetch(self, window, owner_user_id):
        return []


def _clear_users_with_slack_ids(pg_session, *slack_user_ids: str) -> None:
    """pg_session (tests/unit/conftest.py) is a real, non-rolled-back
    Postgres session — a User row this test commits with a fixed
    slack_user_id (unique-constrained: uq_users_slack_user_id) survives
    into the next test run against the same DB, and collides on re-run.
    Random User.id (uuid.uuid4()) doesn't have this problem; slack_user_id
    is the one fixed literal here, so it's the one that needs clearing
    first — found live: a second suite run in the same session failed on
    this exact collision."""
    pg_session.execute(delete(User).where(User.slack_user_id.in_(slack_user_ids)))
    pg_session.commit()


def test_gather_resolves_attendees_and_flags_no_agenda_carryover_for_non_recurring(
    pg_session,
):
    user = User(id=str(uuid.uuid4()), created_at=NOW)
    pg_session.add(user)
    pg_session.commit()
    scope = OwnerScope(owner_user_id=user.id, session=pg_session)
    clock = FrozenClock(at=NOW)

    event = {
        "external_id": "evt-1",
        "title": "Sync with Sam",
        "starts_at": (NOW + datetime.timedelta(minutes=15)).isoformat(),
        "is_recurring": False,
        "attendees": [
            {
                "source": "calendar",
                "external_id": "sam@ext.example.com",
                "email": "sam@ext.example.com",
                "display_name": "Sam External",
            },
        ],
    }
    connectors = {"slack": _FixtureSlackClient(), "linear": _FixtureLinearClient()}

    context = gather_dossier_context(event, scope, clock, connectors)

    assert context.event["external_id"] == "evt-1"
    assert len(context.resolved_attendees) == 1
    assert context.agenda_carryover is None


def test_gather_skips_agenda_carryover_for_recurring_meeting_with_no_matched_attendee(
    pg_session,
):
    """No attendee resolved to a real person -> no Pair can be matched, so
    carryover must stay unset rather than falling back to whatever Pair
    the owner happens to have (the old self-only shortcut this replaces
    would have silently attached an unrelated Pair's agenda here)."""
    user = User(id=str(uuid.uuid4()), created_at=NOW)
    pg_session.add(user)
    pg_session.commit()
    scope = OwnerScope(owner_user_id=user.id, session=pg_session)
    clock = FrozenClock(at=NOW)

    event = {
        "external_id": "evt-2",
        "title": "Weekly 1:1",
        "starts_at": (NOW + datetime.timedelta(minutes=15)).isoformat(),
        "is_recurring": True,
        "attendees": [],
    }
    connectors = {"slack": _FixtureSlackClient(), "linear": _FixtureLinearClient()}

    context = gather_dossier_context(event, scope, clock, connectors)

    assert context.agenda_carryover is None


def test_resolve_pair_scope_for_attendees_matches_report_meeting_their_manager(
    pg_session,
):
    report = User(id=str(uuid.uuid4()), created_at=NOW)
    manager = User(id=str(uuid.uuid4()), created_at=NOW)
    pg_session.add_all([report, manager])
    pg_session.commit()
    pg_session.add(
        Pair(
            id=str(uuid.uuid4()),
            report_user_id=report.id,
            manager_user_id=manager.id,
            started_at=NOW,
            ended_at=None,
        )
    )
    pg_session.commit()
    owner_scope = OwnerScope(owner_user_id=report.id, session=pg_session)

    resolved_attendees = [
        ResolvedAttendee(
            raw={"display_name": "Manager"},
            resolution=Resolved(person_id=manager.id, tier=1, confidence="verified"),
        )
    ]

    pair_scope = _resolve_pair_scope_for_attendees(owner_scope, resolved_attendees)

    assert pair_scope is not None
    assert pair_scope.report_user_id == report.id


def test_attendee_relationship_labels_manager_report_colleague_external(pg_session):
    owner = User(id=str(uuid.uuid4()), created_at=NOW)
    manager = User(id=str(uuid.uuid4()), created_at=NOW)
    report = User(id=str(uuid.uuid4()), created_at=NOW)
    colleague = User(id=str(uuid.uuid4()), created_at=NOW)
    pg_session.add_all([owner, manager, report, colleague])
    pg_session.commit()
    pg_session.add_all(
        [
            Pair(
                id=str(uuid.uuid4()),
                report_user_id=owner.id,
                manager_user_id=manager.id,
                started_at=NOW,
                ended_at=None,
            ),
            Pair(
                id=str(uuid.uuid4()),
                report_user_id=report.id,
                manager_user_id=owner.id,
                started_at=NOW,
                ended_at=None,
            ),
        ]
    )
    pg_session.commit()
    owner_scope = OwnerScope(owner_user_id=owner.id, session=pg_session)

    assert (
        _attendee_relationship(
            owner_scope, Resolved(person_id=manager.id, tier=1, confidence="verified")
        )
        == "manager"
    )
    assert (
        _attendee_relationship(
            owner_scope, Resolved(person_id=report.id, tier=1, confidence="verified")
        )
        == "report"
    )
    assert (
        _attendee_relationship(
            owner_scope, Resolved(person_id=colleague.id, tier=1, confidence="verified")
        )
        == "colleague"
    )
    assert (
        _attendee_relationship(
            owner_scope, Unattributed(reference_key="k", raw_handle=None)
        )
        == "external"
    )


def test_resolve_pair_scope_for_attendees_matches_manager_meeting_their_report(
    pg_session,
):
    report = User(id=str(uuid.uuid4()), created_at=NOW)
    manager = User(id=str(uuid.uuid4()), created_at=NOW)
    pg_session.add_all([report, manager])
    pg_session.commit()
    pg_session.add(
        Pair(
            id=str(uuid.uuid4()),
            report_user_id=report.id,
            manager_user_id=manager.id,
            started_at=NOW,
            ended_at=None,
        )
    )
    pg_session.commit()
    owner_scope = OwnerScope(owner_user_id=manager.id, session=pg_session)

    resolved_attendees = [
        ResolvedAttendee(
            raw={"display_name": "Report"},
            resolution=Resolved(person_id=report.id, tier=1, confidence="verified"),
        )
    ]

    pair_scope = _resolve_pair_scope_for_attendees(owner_scope, resolved_attendees)

    assert pair_scope is not None
    assert pair_scope.report_user_id == report.id


def test_open_commitments_matches_only_resolved_attendees_open_commitments(
    pg_session,
):
    user = User(id=str(uuid.uuid4()), created_at=NOW)
    pg_session.add(user)
    pg_session.commit()
    scope = OwnerScope(owner_user_id=user.id, session=pg_session)

    attendee_person = Person(
        id=str(uuid.uuid4()),
        owner_user_id=user.id,
        canonical_name="Sam External",
        created_at=NOW,
    )
    other_person = Person(
        id=str(uuid.uuid4()),
        owner_user_id=user.id,
        canonical_name="Not Invited",
        created_at=NOW,
    )
    pg_session.add_all([attendee_person, other_person])
    pg_session.commit()

    pg_session.add_all(
        [
            Commitment(
                id=str(uuid.uuid4()),
                owner_user_id=user.id,
                promised_to_person_id=attendee_person.id,
                description="Send the updated timeline",
                source_reference_key="test",
                promised_at=NOW,
                due_at=NOW - datetime.timedelta(days=1),
                delivered_at=None,
                status="open",
            ),
            # Already delivered — must not show up.
            Commitment(
                id=str(uuid.uuid4()),
                owner_user_id=user.id,
                promised_to_person_id=attendee_person.id,
                description="Already delivered",
                source_reference_key="test",
                promised_at=NOW,
                due_at=None,
                delivered_at=NOW,
                status="delivered",
            ),
            # Owed to someone not in this meeting — must not show up.
            Commitment(
                id=str(uuid.uuid4()),
                owner_user_id=user.id,
                promised_to_person_id=other_person.id,
                description="Owed to someone else",
                source_reference_key="test",
                promised_at=NOW,
                due_at=None,
                delivered_at=None,
                status="open",
            ),
        ]
    )
    pg_session.commit()

    resolved_attendees = [
        ResolvedAttendee(
            raw={"display_name": "Sam External"},
            resolution=Resolved(
                person_id=attendee_person.id, tier=1, confidence="verified"
            ),
        ),
        ResolvedAttendee(
            raw={"display_name": "unresolved"},
            resolution=Unattributed(reference_key="k2", raw_handle=None),
        ),
    ]

    result = _open_commitments_with_attendees(scope, resolved_attendees, NOW)

    assert len(result) == 1
    assert result[0]["description"] == "Send the updated timeline"
    assert result[0]["promised_to_name"] == "Sam External"
    assert result[0]["overdue"] is True


def test_blocked_work_items_matches_only_resolved_attendees_blocked_items(pg_session):
    user = User(id=str(uuid.uuid4()), created_at=NOW)
    pg_session.add(user)
    pg_session.commit()
    scope = OwnerScope(owner_user_id=user.id, session=pg_session)

    attendee_person = Person(
        id=str(uuid.uuid4()),
        owner_user_id=user.id,
        canonical_name="Sam External",
        created_at=NOW,
    )
    other_person = Person(
        id=str(uuid.uuid4()),
        owner_user_id=user.id,
        canonical_name="Not Invited",
        created_at=NOW,
    )
    pg_session.add_all([attendee_person, other_person])
    pg_session.commit()

    pg_session.add_all(
        [
            WorkItem(
                id=str(uuid.uuid4()),
                owner_user_id=user.id,
                actor_reference_key="linear:sam@acme.com",
                resolved_person_id=attendee_person.id,
                source="linear",
                external_id="MENT-214",
                title="Fix flaky roster test",
                status="blocked",
                url="https://linear.app/MENT-214",
                updated_at=NOW - datetime.timedelta(days=3),
                blocks_others=False,
            ),
            # Not blocked — must not show up.
            WorkItem(
                id=str(uuid.uuid4()),
                owner_user_id=user.id,
                actor_reference_key="linear:sam@acme.com",
                resolved_person_id=attendee_person.id,
                source="linear",
                external_id="MENT-201",
                title="In progress item",
                status="in_progress",
                url=None,
                updated_at=NOW,
                blocks_others=False,
            ),
            # Blocked, but assigned to someone not in this meeting.
            WorkItem(
                id=str(uuid.uuid4()),
                owner_user_id=user.id,
                actor_reference_key="linear:other@acme.com",
                resolved_person_id=other_person.id,
                source="linear",
                external_id="MENT-999",
                title="Someone else's blocked item",
                status="blocked",
                url=None,
                updated_at=NOW,
                blocks_others=False,
            ),
        ]
    )
    pg_session.commit()

    resolved_attendees = [
        ResolvedAttendee(
            raw={"display_name": "Sam External"},
            resolution=Resolved(
                person_id=attendee_person.id, tier=1, confidence="verified"
            ),
        ),
        ResolvedAttendee(
            raw={"display_name": "unresolved"},
            resolution=Unattributed(reference_key="k2", raw_handle=None),
        ),
    ]

    result = _blocked_work_items_with_attendees(scope, resolved_attendees, NOW)

    assert len(result) == 1
    assert result[0]["external_id"] == "MENT-214"
    assert result[0]["person_name"] == "Sam External"
    assert result[0]["days_stale"] == 3


def test_blocked_work_items_is_empty_with_no_resolved_attendees(pg_session):
    user = User(id=str(uuid.uuid4()), created_at=NOW)
    pg_session.add(user)
    pg_session.commit()
    scope = OwnerScope(owner_user_id=user.id, session=pg_session)

    resolved_attendees = [
        ResolvedAttendee(
            raw={"display_name": "unresolved"},
            resolution=Unattributed(reference_key="k1", raw_handle=None),
        )
    ]

    assert _blocked_work_items_with_attendees(scope, resolved_attendees, NOW) == []


def test_gather_builds_a_real_window_for_connectors(pg_session):
    """Connectors receive a real Window (start/end), not None — a live
    connector like LiveCalendarClient/LiveGmailClient dereferences
    window.start/window.end and would crash on None (see
    app.ingest.live_source)."""
    user = User(id=str(uuid.uuid4()), created_at=NOW)
    pg_session.add(user)
    pg_session.commit()
    scope = OwnerScope(owner_user_id=user.id, session=pg_session)
    clock = FrozenClock(at=NOW)

    seen_windows = []

    class _RecordingClient:
        def fetch(self, window, owner_user_id):
            seen_windows.append(window)
            return []

    event = {
        "external_id": "evt-3",
        "title": "Sync",
        "starts_at": (NOW + datetime.timedelta(minutes=15)).isoformat(),
        "is_recurring": False,
        "attendees": [],
    }
    connectors = {"recording": _RecordingClient()}

    gather_dossier_context(event, scope, clock, connectors)

    assert len(seen_windows) == 1
    window = seen_windows[0]
    # Backward-looking, not forward: Slack/Linear "what's happened since we
    # last talked" signals are retrospective, not a preview of the next 15
    # minutes before the meeting starts (finding 6 of the whole-branch
    # review fix wave).
    assert window.start == NOW - datetime.timedelta(days=7)
    assert window.end == NOW


def test_attendee_slack_user_ids_only_counts_resolved_and_linked_attendees(
    pg_session,
):
    _clear_users_with_slack_ids(pg_session, "U0LINKED")
    owner = User(id=str(uuid.uuid4()), created_at=NOW)
    linked = User(id=str(uuid.uuid4()), created_at=NOW, slack_user_id="U0LINKED")
    unlinked = User(id=str(uuid.uuid4()), created_at=NOW)  # resolved, no Slack link
    pg_session.add_all([owner, linked, unlinked])
    pg_session.commit()
    scope = OwnerScope(owner_user_id=owner.id, session=pg_session)

    resolved_attendees = [
        ResolvedAttendee(
            raw={"display_name": "Linked"},
            resolution=Resolved(person_id=linked.id, tier=1, confidence="verified"),
        ),
        ResolvedAttendee(
            raw={"display_name": "Unlinked"},
            resolution=Resolved(person_id=unlinked.id, tier=1, confidence="verified"),
        ),
        ResolvedAttendee(
            raw={"display_name": "External"},
            resolution=Unattributed(reference_key="calendar:ext@example.com", raw_handle=None),
        ),
    ]

    assert _attendee_slack_user_ids(scope, resolved_attendees) == {"U0LINKED"}


def test_filter_slack_signals_to_attendees_keeps_only_attendee_senders():
    """REAL CHANGE (requested: "only show the messages sent by the
    attendees") — a message merely @-mentioning an attendee, sent by
    someone else entirely, is no longer enough; this is exactly what let
    a bot-delivered card that happens to @-mention an attendee's name
    slip through as if it were a real signal from that attendee."""
    attendee_ids = {"U0ATTENDEE"}
    messages = [
        {"external_id": "1", "actor_reference_key": "slack:U0ATTENDEE", "mentioned_slack_user_ids": []},
        {"external_id": "2", "actor_reference_key": "slack:U0STRANGER", "mentioned_slack_user_ids": ["U0ATTENDEE"]},
        {"external_id": "3", "actor_reference_key": "slack:U0STRANGER", "mentioned_slack_user_ids": ["U0OTHER"]},
    ]

    filtered = _filter_slack_signals_to_attendees(messages, attendee_ids)

    assert {m["external_id"] for m in filtered} == {"1"}


def test_filter_slack_signals_to_attendees_sorts_owner_mentions_first():
    """REAL CHANGE (requested: "tagging the user preferably but not
    obligatory") — a preference, not a filter: both messages are kept
    (both sent by the attendee), but the one that also @-mentions the
    dossier owner sorts first."""
    attendee_ids = {"U0ATTENDEE"}
    messages = [
        {"external_id": "no-owner-mention", "actor_reference_key": "slack:U0ATTENDEE", "mentioned_slack_user_ids": []},
        {"external_id": "mentions-owner", "actor_reference_key": "slack:U0ATTENDEE", "mentioned_slack_user_ids": ["U0OWNER"]},
    ]

    filtered = _filter_slack_signals_to_attendees(
        messages, attendee_ids, owner_slack_user_id="U0OWNER"
    )

    assert [m["external_id"] for m in filtered] == ["mentions-owner", "no-owner-mention"]


def test_filter_slack_signals_to_attendees_is_empty_with_no_attendee_ids():
    messages = [
        {"external_id": "1", "actor_reference_key": "slack:U0ANYONE", "mentioned_slack_user_ids": []}
    ]

    assert _filter_slack_signals_to_attendees(messages, set()) == []


def test_gather_dossier_context_filters_slack_signal_to_this_meetings_attendee(
    pg_session,
):
    _clear_users_with_slack_ids(pg_session, "U0ATTENDEE")
    owner = User(id=str(uuid.uuid4()), created_at=NOW)
    attendee_user = User(
        id=str(uuid.uuid4()), created_at=NOW, slack_user_id="U0ATTENDEE"
    )
    pg_session.add_all([owner, attendee_user])
    pg_session.commit()
    attendee_person = Person(
        id=attendee_user.id,
        owner_user_id=owner.id,
        canonical_name="Attendee Person",
        primary_email="attendee@example.com",
        created_at=NOW,
    )
    pg_session.add(attendee_person)
    pg_session.commit()
    scope = OwnerScope(owner_user_id=owner.id, session=pg_session)
    clock = FrozenClock(at=NOW)

    class _SlackWithMixedRelevance:
        def fetch(self, window, owner_user_id):
            return [
                {
                    "external_id": "relevant-1",
                    "actor_reference_key": "slack:U0ATTENDEE",
                    "mentioned_slack_user_ids": [],
                },
                {
                    "external_id": "irrelevant-1",
                    "actor_reference_key": "slack:U0RANDOM",
                    "mentioned_slack_user_ids": [],
                },
            ]

    event = {
        "external_id": "evt-slack-filter",
        "title": "1:1",
        "starts_at": (NOW + datetime.timedelta(minutes=15)).isoformat(),
        "is_recurring": False,
        "attendees": [
            {
                "source": "calendar",
                "email": "attendee@example.com",
                "display_name": "Attendee Person",
            }
        ],
    }
    connectors = {
        "slack": _SlackWithMixedRelevance(),
        "linear": _FixtureLinearClient(),
    }

    context = gather_dossier_context(event, scope, clock, connectors)

    assert [m["external_id"] for m in context.raw_signals["slack"]] == ["relevant-1"]
