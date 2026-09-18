import datetime
import uuid

from app.agenda.models import Pair
from app.core.clock import FrozenClock
from app.core.models import User
from app.identity.models import Person
from app.ingest.notion_pair_sync import sync_notion_pair_edge

NOW = datetime.datetime(2026, 8, 18, tzinfo=datetime.UTC)


def _linked_user(db_session, notion_owner_email: str) -> str:
    uid = str(uuid.uuid4())
    db_session.add(
        User(id=uid, created_at=NOW, notion_owner_email=notion_owner_email)
    )
    db_session.commit()
    return uid


def _email(label: str) -> str:
    # notion_owner_email is globally unique (uq_users_notion_owner_email)
    # and this suite runs against a shared, non-transactional Postgres
    # instance — suffix every literal email with a fresh uuid so parallel
    # test functions never collide, same convention as
    # test_hris_normalize.py's _emp_id.
    return f"{label}-{uuid.uuid4()}@example.com"


def test_sync_creates_a_pair_when_both_sides_are_linked(db_session):
    report_email, manager_email = _email("report"), _email("manager")
    report = _linked_user(db_session, report_email)
    manager = _linked_user(db_session, manager_email)
    raw = {
        "report_notion_email": report_email,
        "manager_notion_email": manager_email,
    }

    pair = sync_notion_pair_edge(db_session, raw, FrozenClock(at=NOW))

    assert pair is not None
    assert pair.report_user_id == report
    assert pair.manager_user_id == manager
    assert pair.ended_at is None


def test_sync_returns_none_when_report_not_linked(db_session):
    report_email, manager_email = _email("report"), _email("manager")
    _linked_user(db_session, manager_email)  # only the manager is linked
    raw = {
        "report_notion_email": report_email,
        "manager_notion_email": manager_email,
    }

    result = sync_notion_pair_edge(db_session, raw, FrozenClock(at=NOW))

    assert result is None
    assert (
        db_session.query(User).filter_by(notion_owner_email=report_email).count() == 0
    )


def test_sync_is_idempotent(db_session):
    report_email, manager_email = _email("report"), _email("manager")
    report = _linked_user(db_session, report_email)
    _linked_user(db_session, manager_email)
    raw = {
        "report_notion_email": report_email,
        "manager_notion_email": manager_email,
    }

    sync_notion_pair_edge(db_session, raw, FrozenClock(at=NOW))
    sync_notion_pair_edge(db_session, raw, FrozenClock(at=NOW))

    assert db_session.query(Pair).filter_by(report_user_id=report).count() == 1


def test_sync_creates_person_rows_so_each_side_can_resolve_the_other(db_session):
    report_email, manager_email = _email("report"), _email("manager")
    report = _linked_user(db_session, report_email)
    manager = _linked_user(db_session, manager_email)
    raw = {
        "report_notion_email": report_email,
        "manager_notion_email": manager_email,
    }

    sync_notion_pair_edge(db_session, raw, FrozenClock(at=NOW))

    # Under the report's own roster: a Person row representing the manager.
    manager_as_seen_by_report = db_session.get(Person, manager)
    assert manager_as_seen_by_report is not None
    assert manager_as_seen_by_report.owner_user_id == report
    assert manager_as_seen_by_report.primary_email == manager_email

    # Under the manager's own roster: a Person row representing the report.
    report_as_seen_by_manager = db_session.get(Person, report)
    assert report_as_seen_by_manager is not None
    assert report_as_seen_by_manager.owner_user_id == manager
    assert report_as_seen_by_manager.primary_email == report_email


def test_sync_person_rows_are_idempotent_on_resync(db_session):
    report_email, manager_email = _email("report"), _email("manager")
    report = _linked_user(db_session, report_email)
    manager = _linked_user(db_session, manager_email)
    raw = {
        "report_notion_email": report_email,
        "manager_notion_email": manager_email,
    }

    sync_notion_pair_edge(db_session, raw, FrozenClock(at=NOW))
    sync_notion_pair_edge(db_session, raw, FrozenClock(at=NOW))

    assert db_session.query(Person).filter_by(id=manager).count() == 1
    assert db_session.query(Person).filter_by(id=report).count() == 1


def test_sync_closes_prior_pair_on_manager_change(db_session):
    report_email, manager_a_email, manager_b_email = (
        _email("report"),
        _email("manager-a"),
        _email("manager-b"),
    )
    report = _linked_user(db_session, report_email)
    manager_a = _linked_user(db_session, manager_a_email)
    manager_b = _linked_user(db_session, manager_b_email)

    first = sync_notion_pair_edge(
        db_session,
        {"report_notion_email": report_email, "manager_notion_email": manager_a_email},
        FrozenClock(at=NOW),
    )

    later = NOW + datetime.timedelta(days=30)
    second = sync_notion_pair_edge(
        db_session,
        {"report_notion_email": report_email, "manager_notion_email": manager_b_email},
        FrozenClock(at=later),
    )

    db_session.refresh(first)
    assert first.ended_at == later
    assert first.manager_user_id == manager_a
    assert second.manager_user_id == manager_b
    assert second.ended_at is None
    assert db_session.query(Pair).filter_by(report_user_id=report).count() == 2
