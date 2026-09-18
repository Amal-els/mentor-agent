import datetime
import uuid

from app.agenda.models import Accomplishment, AgendaItem, Pair
from app.core.clock import FrozenClock
from app.core.models import Commitment, Goal, PulseDelivery, User, WorkItem
from app.core.scope import OwnerScope
from app.sub_agents.friday_review.sub_agents.gather.agent import (
    gather_friday_review_context,
    week_start_monday,
)

NOW = datetime.datetime(2026, 8, 28, 16, 0, 0, tzinfo=datetime.UTC)  # a Friday


def _make_owner(pg_session) -> User:
    user = User(id=str(uuid.uuid4()), created_at=NOW)
    pg_session.add(user)
    pg_session.commit()
    return user


def test_week_start_monday_from_a_friday():
    assert week_start_monday(NOW) == datetime.date(2026, 8, 24)


def test_wins_include_accomplishments_from_past_week_already_logged(pg_session):
    user = _make_owner(pg_session)
    scope = OwnerScope(owner_user_id=user.id, session=pg_session)
    pg_session.add(
        Accomplishment(
            id=str(uuid.uuid4()), owner_user_id=user.id, description="Shipped X",
            source_reference_key="acc-1", occurred_at=NOW - datetime.timedelta(days=2),
        )
    )
    pg_session.commit()

    context = gather_friday_review_context(scope, FrozenClock(at=NOW))

    wins = [w for w in context.wins if w.source_reference_key == "acc-1"]
    assert len(wins) == 1
    assert wins[0].already_logged is True


def test_wins_include_delivered_commitments_not_yet_logged(pg_session):
    user = _make_owner(pg_session)
    scope = OwnerScope(owner_user_id=user.id, session=pg_session)
    commitment_id = str(uuid.uuid4())
    pg_session.add(
        Commitment(
            id=commitment_id, owner_user_id=user.id, promised_to_person_id=None,
            description="Deliver the report", source_reference_key="src-1",
            promised_at=NOW - datetime.timedelta(days=10),
            due_at=NOW - datetime.timedelta(days=3),
            delivered_at=NOW - datetime.timedelta(days=2), status="delivered",
        )
    )
    pg_session.commit()

    context = gather_friday_review_context(scope, FrozenClock(at=NOW))

    wins = [w for w in context.wins if w.source_reference_key == commitment_id]
    assert len(wins) == 1
    assert wins[0].already_logged is False
    assert wins[0].kind == "commitment"


def test_wins_include_closed_work_items_not_yet_logged(pg_session):
    user = _make_owner(pg_session)
    scope = OwnerScope(owner_user_id=user.id, session=pg_session)
    item_id = str(uuid.uuid4())
    pg_session.add(
        WorkItem(
            id=item_id, owner_user_id=user.id, actor_reference_key="me",
            resolved_person_id=None, source="linear", external_id="LIN-1",
            title="Migrate the pipeline", status="Done", url="https://linear.app/x/1",
            due_at=None, updated_at=NOW - datetime.timedelta(days=1),
        )
    )
    pg_session.commit()

    context = gather_friday_review_context(scope, FrozenClock(at=NOW))

    wins = [w for w in context.wins if w.source_reference_key == item_id]
    assert len(wins) == 1
    assert wins[0].already_logged is False
    assert wins[0].source_link == "https://linear.app/x/1"


def test_open_work_item_is_not_a_win(pg_session):
    user = _make_owner(pg_session)
    scope = OwnerScope(owner_user_id=user.id, session=pg_session)
    pg_session.add(
        WorkItem(
            id=str(uuid.uuid4()), owner_user_id=user.id, actor_reference_key="me",
            resolved_person_id=None, source="linear", external_id="LIN-2",
            title="Still in progress", status="In Progress", url="https://x/2",
            due_at=None, updated_at=NOW - datetime.timedelta(days=1),
        )
    )
    pg_session.commit()

    context = gather_friday_review_context(scope, FrozenClock(at=NOW))
    assert context.wins == []


def test_slipped_is_overdue_open_commitment(pg_session):
    user = _make_owner(pg_session)
    scope = OwnerScope(owner_user_id=user.id, session=pg_session)
    pg_session.add(
        Commitment(
            id=str(uuid.uuid4()), owner_user_id=user.id, promised_to_person_id=None,
            description="Send the doc", source_reference_key="src-2",
            promised_at=NOW - datetime.timedelta(days=10),
            due_at=NOW - datetime.timedelta(days=1), delivered_at=None, status="open",
        )
    )
    pg_session.commit()

    context = gather_friday_review_context(scope, FrozenClock(at=NOW))
    assert len(context.slipped) == 1
    assert context.slipped[0].description == "Send the doc"


def test_three_week_pattern_boundary(pg_session):
    user = _make_owner(pg_session)
    scope = OwnerScope(owner_user_id=user.id, session=pg_session)
    pg_session.add_all(
        [
            Commitment(
                id=str(uuid.uuid4()), owner_user_id=user.id, promised_to_person_id=None,
                description="Old enough", source_reference_key="src-old",
                promised_at=NOW - datetime.timedelta(days=21),
                due_at=NOW - datetime.timedelta(days=1),
                delivered_at=None, status="open",
            ),
            Commitment(
                id=str(uuid.uuid4()), owner_user_id=user.id, promised_to_person_id=None,
                description="Not old enough", source_reference_key="src-new",
                promised_at=NOW - datetime.timedelta(days=20),
                due_at=NOW - datetime.timedelta(days=1),
                delivered_at=None, status="open",
            ),
        ]
    )
    pg_session.commit()

    context = gather_friday_review_context(scope, FrozenClock(at=NOW))
    by_desc = {s.description: s.weeks_running for s in context.slipped}
    assert by_desc["Old enough"] is True
    assert by_desc["Not old enough"] is False


def test_agenda_resolved_carried_and_stuck(pg_session):
    user = _make_owner(pg_session)
    manager = User(id=str(uuid.uuid4()), created_at=NOW)
    pg_session.add(manager)
    pg_session.commit()
    pair = Pair(
        id=str(uuid.uuid4()), report_user_id=user.id, manager_user_id=manager.id,
        started_at=NOW - datetime.timedelta(days=100), ended_at=None,
    )
    pg_session.add(pair)
    pg_session.commit()
    pg_session.add_all(
        [
            AgendaItem(
                id=str(uuid.uuid4()), report_user_id=user.id, pair_id=pair.id,
                text="Resolved this week", source="manual", source_link=None,
                visibility="shared", status="resolved", surfaced_count=1,
                created_at=NOW - datetime.timedelta(days=5),
                resolved_at=NOW - datetime.timedelta(days=1),
                created_by_user_id=user.id, created_by_role="report",
            ),
            AgendaItem(
                id=str(uuid.uuid4()), report_user_id=user.id, pair_id=pair.id,
                text="Still carried", source="manual", source_link=None,
                visibility="shared", status="open", surfaced_count=1,
                created_at=NOW - datetime.timedelta(days=5), resolved_at=None,
                created_by_user_id=user.id, created_by_role="report",
            ),
            AgendaItem(
                id=str(uuid.uuid4()), report_user_id=user.id, pair_id=pair.id,
                text="Stuck", source="manual", source_link=None,
                visibility="shared", status="open", surfaced_count=3,
                created_at=NOW - datetime.timedelta(days=20), resolved_at=None,
                created_by_user_id=user.id, created_by_role="report",
            ),
        ]
    )
    pg_session.commit()
    scope = OwnerScope(owner_user_id=user.id, session=pg_session)

    context = gather_friday_review_context(scope, FrozenClock(at=NOW))

    assert [i.text for i in context.agenda.resolved_this_week] == ["Resolved this week"]
    carried_texts = {i.text for i in context.agenda.carried}
    assert carried_texts == {"Still carried", "Stuck"}
    assert [i.text for i in context.agenda.stuck] == ["Stuck"]


def test_okr_progress_and_career_goal(pg_session):
    user = _make_owner(pg_session)
    scope = OwnerScope(owner_user_id=user.id, session=pg_session)
    pg_session.add_all(
        [
            Goal(
                id=str(uuid.uuid4()), owner_user_id=user.id, title="Ship the migration",
                status="active", external_ref=None, created_at=NOW,
                goal_type="key_result", progress=0.6, current_value=6, target_value=10,
            ),
            Goal(
                id=str(uuid.uuid4()), owner_user_id=user.id, title="Become a tech lead",
                status="active", external_ref=None, created_at=NOW,
                goal_type="career_goal", progress=None, current_value=None, target_value=None,
            ),
        ]
    )
    pg_session.commit()

    context = gather_friday_review_context(scope, FrozenClock(at=NOW))
    assert len(context.okr_progress) == 1
    assert context.okr_progress[0].title == "Ship the migration"
    assert context.career_goal is not None
    assert context.career_goal.title == "Become a tech lead"


def test_skill_distribution_only_categorized_within_trailing_8_weeks(pg_session):
    user = _make_owner(pg_session)
    scope = OwnerScope(owner_user_id=user.id, session=pg_session)
    pg_session.add_all(
        [
            Accomplishment(
                id=str(uuid.uuid4()), owner_user_id=user.id, description="a",
                source_reference_key="a", occurred_at=NOW - datetime.timedelta(days=10),
                skill_category="technical_execution",
            ),
            Accomplishment(
                id=str(uuid.uuid4()), owner_user_id=user.id, description="b",
                source_reference_key="b", occurred_at=NOW - datetime.timedelta(days=10),
                skill_category=None,
            ),
            Accomplishment(
                id=str(uuid.uuid4()), owner_user_id=user.id, description="c",
                source_reference_key="c", occurred_at=NOW - datetime.timedelta(days=60),
                skill_category="technical_execution",
            ),
        ]
    )
    pg_session.commit()

    context = gather_friday_review_context(scope, FrozenClock(at=NOW))
    assert context.skill_distribution.counts == {"technical_execution": 1}


def test_daily_pulse_pattern_requires_three_distinct_days(pg_session):
    user = _make_owner(pg_session)
    scope = OwnerScope(owner_user_id=user.id, session=pg_session)
    for i in range(3):
        pg_session.add(
            PulseDelivery(
                id=str(uuid.uuid4()), owner_user_id=user.id, ritual="pulse",
                local_date=NOW.date() - datetime.timedelta(days=i), trigger="cron",
                delivered_at=NOW - datetime.timedelta(days=i), item_ids=["item-x"],
                context_hash="h", prompt_version="p",
            )
        )
    for i in range(3, 5):
        pg_session.add(
            PulseDelivery(
                id=str(uuid.uuid4()), owner_user_id=user.id, ritual="pulse",
                local_date=NOW.date() - datetime.timedelta(days=i), trigger="cron",
                delivered_at=NOW - datetime.timedelta(days=i), item_ids=["item-y"],
                context_hash="h", prompt_version="p",
            )
        )
    pg_session.commit()

    context = gather_friday_review_context(scope, FrozenClock(at=NOW))
    assert context.daily_pulse_patterns == ["item-x"]


def test_identity_batch_is_empty_when_nothing_pending(pg_session):
    user = _make_owner(pg_session)
    scope = OwnerScope(owner_user_id=user.id, session=pg_session)
    context = gather_friday_review_context(scope, FrozenClock(at=NOW))
    assert context.identity_batch == []


def test_cross_owner_isolation(pg_session):
    owner_a = _make_owner(pg_session)
    owner_b = _make_owner(pg_session)
    pg_session.add(
        Accomplishment(
            id=str(uuid.uuid4()), owner_user_id=owner_b.id, description="b's win",
            source_reference_key="acc-b", occurred_at=NOW - datetime.timedelta(days=1),
        )
    )
    pg_session.commit()

    scope_a = OwnerScope(owner_user_id=owner_a.id, session=pg_session)
    context = gather_friday_review_context(scope_a, FrozenClock(at=NOW))
    assert context.wins == []
