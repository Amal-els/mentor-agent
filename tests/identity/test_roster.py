import datetime
import uuid

from app.identity.models import NotSameAs, Person, RosterVersion
from app.identity.roster import load_snapshot


def test_load_snapshot_reads_version_and_data_atomically(db_session, make_scope):
    scope = make_scope()
    db_session.add(RosterVersion(owner_user_id=scope.owner_user_id, version=3))
    person = Person(
        id=str(uuid.uuid4()),
        owner_user_id=scope.owner_user_id,
        canonical_name="Sarah Ben Youssef",
        primary_email="sarah@acme.com",
        is_self=False,
        is_active=True,
        created_at=datetime.datetime.now(datetime.UTC),
    )
    db_session.add(person)
    db_session.commit()
    db_session.add(
        NotSameAs(
            id=str(uuid.uuid4()),
            owner_user_id=scope.owner_user_id,
            reference_key="slack:handle:sbenali",
            person_id=person.id,
            rejected_at=datetime.datetime.now(datetime.UTC),
            actor="user",
        )
    )
    db_session.commit()

    snapshot = load_snapshot(scope)

    assert snapshot.roster_version == 3
    assert len(snapshot.people) == 1
    assert snapshot.people[0].id == person.id
    assert ("slack:handle:sbenali", person.id) in snapshot.not_same_as


def test_load_snapshot_defaults_version_to_zero_when_unset(db_session, make_scope):
    scope = make_scope()
    snapshot = load_snapshot(scope)
    assert snapshot.roster_version == 0
    assert snapshot.people == []


def test_load_snapshot_never_returns_another_owners_people(db_session, make_scope):
    scope_a = make_scope()
    scope_b = make_scope()
    db_session.add(
        Person(
            id=str(uuid.uuid4()),
            owner_user_id=scope_a.owner_user_id,
            canonical_name="Owner A's Person",
            primary_email=None,
            is_self=False,
            is_active=True,
            created_at=datetime.datetime.now(datetime.UTC),
        )
    )
    db_session.commit()

    snapshot_b = load_snapshot(scope_b)

    assert snapshot_b.people == []
