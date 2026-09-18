import datetime

from app.core.clock import FrozenClock
from app.core.models import ChecklistCompletion, Message, PulseDelivery, User, WorkItem
from app.core.scope import OwnerScope
from app.ingest.fixture_source import (
    FixtureCalendarClient,
    FixtureLinearClient,
    FixtureSlackClient,
    load_day_fixture,
)
from app.ingest.seed import seed_fixture
from app.salience.pulse.assemble import assemble_and_score


def _clients(fixture_name):
    fixture = load_day_fixture(fixture_name)
    return [
        FixtureCalendarClient(fixture),
        FixtureSlackClient(fixture),
        FixtureLinearClient(fixture),
    ]


def test_normal_day_shortlist_and_focus_candidates(pg_session):
    counts = seed_fixture(pg_session, "normal_day")
    scope = OwnerScope(owner_user_id=counts["owner_user_id"], session=pg_session)
    clock = FrozenClock(at=datetime.datetime(2026, 8, 7, 7, 0, tzinfo=datetime.UTC))

    ctx = assemble_and_score(scope, clock, _clients("normal_day"))

    # 3 events + 2 work items + Sarah's DM (now that Message rows are
    # scored too — see app/salience/score.py's score_message). No
    # pre-ranker cap anymore (see config.py) — this is normal_day's full
    # candidate count, not a truncation.
    assert len(ctx.shortlist) == 6
    assert ctx.degraded_sources == []
    assert len(ctx.owed) == 1
    assert ctx.owed[0].overdue is True

    # evt-1 (1:1 w/ Sarah), evt-3 (design review w/ Marc — also in the
    # roster), MENT-214 (blocked on Sarah, due today), and Sarah's DM
    # (sent today, resolves to Sarah) all resolve a real person and carry
    # urgency; evt-2 (unresolved standup actor) and MENT-201 (in-progress,
    # due next week) don't.
    focus_ids = {item.item_id for item in ctx.shortlist if item.candidate_focus}
    assert len(focus_ids) == 4


def test_limit_none_returns_every_scored_candidate_uncapped(pg_session):
    counts = seed_fixture(pg_session, "normal_day")
    scope = OwnerScope(owner_user_id=counts["owner_user_id"], session=pg_session)
    clock = FrozenClock(at=datetime.datetime(2026, 8, 7, 7, 0, tzinfo=datetime.UTC))

    default_ctx = assemble_and_score(scope, clock, _clients("normal_day"))
    capped_ctx = assemble_and_score(scope, clock, _clients("normal_day"), limit=2)
    uncapped_ctx = assemble_and_score(scope, clock, _clients("normal_day"), limit=None)

    assert len(default_ctx.shortlist) == 6  # default is uncapped now, same as limit=None
    assert len(capped_ctx.shortlist) == 2
    assert len(uncapped_ctx.shortlist) == 6  # every candidate, explicit limit=None


def test_checklist_completed_items_are_excluded_from_the_shortlist(pg_session):
    counts = seed_fixture(pg_session, "normal_day")
    scope = OwnerScope(owner_user_id=counts["owner_user_id"], session=pg_session)
    clock = FrozenClock(at=datetime.datetime(2026, 8, 7, 7, 0, tzinfo=datetime.UTC))
    work_item = pg_session.execute(scope.query(WorkItem)).scalars().first()
    message = pg_session.execute(scope.query(Message)).scalars().first()
    assert work_item is not None
    assert message is not None
    pg_session.add_all(
        [
            ChecklistCompletion(
                id="complete-work-item",
                owner_user_id=scope.owner_user_id,
                item_type="work_item",
                source=work_item.source,
                external_id=work_item.external_id,
                completed_at=clock.now(),
                ai_response="Done.",
            ),
            ChecklistCompletion(
                id="complete-message",
                owner_user_id=scope.owner_user_id,
                item_type="message",
                source=message.source,
                external_id=message.external_id,
                completed_at=clock.now(),
                ai_response="Handled.",
            ),
        ]
    )
    pg_session.commit()

    ctx = assemble_and_score(scope, clock, _clients("normal_day"))

    shortlist_ids = {item.item_id for item in ctx.shortlist}
    assert work_item.id not in shortlist_ids
    assert message.id not in shortlist_ids


def test_owner_slack_messages_are_excluded_from_the_shortlist(pg_session):
    counts = seed_fixture(pg_session, "normal_day")
    scope = OwnerScope(owner_user_id=counts["owner_user_id"], session=pg_session)
    clock = FrozenClock(at=datetime.datetime(2026, 8, 7, 7, 0, tzinfo=datetime.UTC))
    owner = pg_session.get(User, scope.owner_user_id)
    assert owner is not None
    owner.slack_user_id = "U0OWNER"
    own_message = Message(
        id="owner-slack-message",
        owner_user_id=scope.owner_user_id,
        actor_reference_key="slack:U0OWNER",
        resolved_person_id=None,
        source="slack",
        external_id="1700000000.000001",
        channel="C0123",
        sent_at=clock.now(),
        is_dm=False,
        body_ref="slack://C0123/1700000000.000001",
    )
    pg_session.add(own_message)
    pg_session.commit()

    ctx = assemble_and_score(scope, clock, _clients("normal_day"))

    assert own_message.id not in {item.item_id for item in ctx.shortlist}


def test_shortlist_is_sorted_descending_by_score(pg_session):
    counts = seed_fixture(pg_session, "normal_day")
    scope = OwnerScope(owner_user_id=counts["owner_user_id"], session=pg_session)
    clock = FrozenClock(at=datetime.datetime(2026, 8, 7, 7, 0, tzinfo=datetime.UTC))

    ctx = assemble_and_score(scope, clock, _clients("normal_day"))

    scores = [item.score for item in ctx.shortlist]
    assert scores == sorted(scores, reverse=True)


def test_clear_day_has_empty_shortlist_and_one_owed(pg_session):
    counts = seed_fixture(pg_session, "clear_day")
    scope = OwnerScope(owner_user_id=counts["owner_user_id"], session=pg_session)
    clock = FrozenClock(at=datetime.datetime(2026, 8, 8, 7, 0, tzinfo=datetime.UTC))

    ctx = assemble_and_score(scope, clock, _clients("clear_day"))

    assert ctx.shortlist == []
    assert len(ctx.owed) == 1


def test_degraded_source_day_reports_both_unhealthy_sources_and_stays_sparse(
    pg_session,
):
    counts = seed_fixture(pg_session, "degraded_source")
    scope = OwnerScope(owner_user_id=counts["owner_user_id"], session=pg_session)
    clock = FrozenClock(at=datetime.datetime(2026, 8, 7, 7, 0, tzinfo=datetime.UTC))

    ctx = assemble_and_score(scope, clock, _clients("degraded_source"))

    # calendar/linear are unhealthy, so seed skipped ingesting them — nothing
    # from those sources for L5 to score even though the fixture *declares*
    # a work item. Slack is healthy in this fixture though, and its one
    # message (now that Message rows are scored — see score_message) still
    # makes it into the shortlist.
    assert len(ctx.shortlist) == 1
    assert ctx.shortlist[0].item_type == "message"
    assert ctx.degraded_sources == ["calendar", "linear"]


def test_series_suppression_day_shortlist_size(pg_session):
    counts = seed_fixture(pg_session, "series_suppression")
    scope = OwnerScope(owner_user_id=counts["owner_user_id"], session=pg_session)
    clock = FrozenClock(at=datetime.datetime(2026, 8, 7, 7, 0, tzinfo=datetime.UTC))

    ctx = assemble_and_score(scope, clock, _clients("series_suppression"))

    assert len(ctx.shortlist) == 3  # 2 events + 1 work item
    assert ctx.degraded_sources == []


def test_day_events_includes_all_events_not_just_the_shortlist(pg_session):
    counts = seed_fixture(pg_session, "normal_day")
    scope = OwnerScope(owner_user_id=counts["owner_user_id"], session=pg_session)
    clock = FrozenClock(at=datetime.datetime(2026, 8, 7, 7, 0, tzinfo=datetime.UTC))

    ctx = assemble_and_score(scope, clock, _clients("normal_day"))

    # all 3 events land in day_events, chronological, regardless of score/cut
    assert len(ctx.day_events) == 3
    starts = [e.starts_at for e in ctx.day_events]
    assert starts == sorted(starts)
    assert all(e.has_dossier is False for e in ctx.day_events)


def test_day_events_excludes_work_items_and_commitments(pg_session):
    counts = seed_fixture(pg_session, "normal_day")
    scope = OwnerScope(owner_user_id=counts["owner_user_id"], session=pg_session)
    clock = FrozenClock(at=datetime.datetime(2026, 8, 7, 7, 0, tzinfo=datetime.UTC))

    ctx = assemble_and_score(scope, clock, _clients("normal_day"))

    day_ids = {e.item_id for e in ctx.day_events}
    shortlist_event_ids = {
        item.item_id for item in ctx.shortlist if item.item_type == "event"
    }
    assert (
        day_ids == shortlist_event_ids
    )  # normal_day's 3 events all fit the shortlist too


def test_meeting_today_flags_items_sharing_a_person_with_a_todays_event(pg_session):
    """normal_day: evt-1 (1:1 with Sarah) is today; MENT-214 (blocked on
    Sarah) and Sarah's Slack DM both resolve to the same person and should
    be flagged meeting_today=True. MENT-201 resolves to the owner, who
    organizes no event today, so it stays False."""
    counts = seed_fixture(pg_session, "normal_day")
    scope = OwnerScope(owner_user_id=counts["owner_user_id"], session=pg_session)
    clock = FrozenClock(at=datetime.datetime(2026, 8, 7, 7, 0, tzinfo=datetime.UTC))

    ctx = assemble_and_score(scope, clock, _clients("normal_day"))

    work_item = next(
        item
        for item in ctx.shortlist
        if item.item_type == "work_item" and item.score_terms["blocked"] is True
    )
    message = next(item for item in ctx.shortlist if item.item_type == "message")
    unrelated_work_item = next(
        item
        for item in ctx.shortlist
        if item.item_type == "work_item" and item.score_terms["blocked"] is False
    )

    assert work_item.score_terms["meeting_today"] is True
    assert message.score_terms["meeting_today"] is True
    assert unrelated_work_item.score_terms["meeting_today"] is False


def test_is_new_since_last_pulse_false_once_a_later_pulse_was_delivered(pg_session):
    counts = seed_fixture(pg_session, "normal_day")
    scope = OwnerScope(owner_user_id=counts["owner_user_id"], session=pg_session)
    clock = FrozenClock(at=datetime.datetime(2026, 8, 7, 7, 0, tzinfo=datetime.UTC))

    # Before any delivery exists, everything is "new" (events don't carry
    # this term at all — "source recency" has no Event equivalent, see
    # score.py's module comments).
    ctx_before = assemble_and_score(scope, clock, _clients("normal_day"))
    assert all(
        item.score_terms["is_new_since_last_pulse"] is True
        for item in ctx_before.shortlist
        if item.item_type != "event"
    )

    pg_session.add(
        PulseDelivery(
            id="pd-test-1",
            owner_user_id=counts["owner_user_id"],
            ritual="pulse",
            local_date=datetime.date(2026, 8, 7),
            trigger="pull",
            delivered_at=datetime.datetime(2026, 8, 7, 8, 0, tzinfo=datetime.UTC),
            item_ids=[],
            context_hash="irrelevant",
            prompt_version="v1",
        )
    )
    pg_session.commit()

    ctx_after = assemble_and_score(scope, clock, _clients("normal_day"))
    message = next(item for item in ctx_after.shortlist if item.item_type == "message")

    # normal_day's Slack DM is sent_at 06:40 local — before the 08:00 UTC
    # delivery above — so it's no longer "new".
    assert message.score_terms["is_new_since_last_pulse"] is False


def test_clear_day_has_no_day_events(pg_session):
    counts = seed_fixture(pg_session, "clear_day")
    scope = OwnerScope(owner_user_id=counts["owner_user_id"], session=pg_session)
    clock = FrozenClock(at=datetime.datetime(2026, 8, 8, 7, 0, tzinfo=datetime.UTC))

    ctx = assemble_and_score(scope, clock, _clients("clear_day"))

    assert ctx.day_events == []
