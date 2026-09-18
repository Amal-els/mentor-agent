import datetime
import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from app.identity.models import Identity, Person


def _make_person(session, scope, **overrides):
    person = Person(
        id=str(uuid.uuid4()),
        canonical_name=overrides.get("canonical_name", "Sarah Ben Youssef"),
        primary_email=overrides.get("primary_email"),
        is_self=overrides.get("is_self", False),
        is_active=overrides.get("is_active", True),
        roster_source=overrides.get("roster_source"),
        created_at=datetime.datetime.now(datetime.UTC),
    )
    scope.add(person)
    scope.commit()
    return person


def test_person_primary_email_unique_per_owner_when_set(db_session, make_scope):
    scope = make_scope()
    _make_person(db_session, scope, primary_email="sarah@acme.com")
    dup = Person(
        id=str(uuid.uuid4()),
        owner_user_id=scope.owner_user_id,
        canonical_name="Someone Else",
        primary_email="sarah@acme.com",
        is_self=False,
        is_active=True,
        created_at=datetime.datetime.now(datetime.UTC),
    )
    db_session.add(dup)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_person_primary_email_can_repeat_across_different_owners(
    db_session, make_scope
):
    scope_a = make_scope()
    scope_b = make_scope()
    _make_person(db_session, scope_a, primary_email="sarah@acme.com")
    # same email, different owner — must succeed, these are unrelated people
    _make_person(db_session, scope_b, primary_email="sarah@acme.com")


def test_identity_source_external_id_unique_per_owner(db_session, make_scope):
    scope = make_scope()
    person = _make_person(db_session, scope)
    now = datetime.datetime.now(datetime.UTC)
    scope.add(
        Identity(
            id=str(uuid.uuid4()),
            person_id=person.id,
            source="slack",
            external_id="U123",
            reference_key="slack:U123",
            key_version=1,
            tier=1,
            confidence="verified",
            verified_by="auto",
            first_seen=now,
            last_seen=now,
        )
    )
    scope.commit()
    dup = Identity(
        id=str(uuid.uuid4()),
        owner_user_id=scope.owner_user_id,
        person_id=person.id,
        source="slack",
        external_id="U123",
        reference_key="slack:U123-other",
        key_version=1,
        tier=1,
        confidence="verified",
        verified_by="auto",
        first_seen=now,
        last_seen=now,
    )
    db_session.add(dup)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_identity_source_external_id_can_repeat_across_different_owners(
    db_session, make_scope
):
    # the constraint test the multi-tenancy revision exists for: the same
    # (source, external_id) — e.g. two users whose Slack workspaces both
    # assigned user ID "U123" — must NOT collapse onto one Identity/Person
    scope_a = make_scope()
    scope_b = make_scope()
    person_a = _make_person(db_session, scope_a)
    person_b = _make_person(db_session, scope_b)
    now = datetime.datetime.now(datetime.UTC)

    scope_a.add(
        Identity(
            id=str(uuid.uuid4()),
            person_id=person_a.id,
            source="slack",
            external_id="U123",
            reference_key="slack:U123",
            key_version=1,
            tier=1,
            confidence="verified",
            verified_by="auto",
            first_seen=now,
            last_seen=now,
        )
    )
    scope_a.commit()
    scope_b.add(
        Identity(
            id=str(uuid.uuid4()),
            person_id=person_b.id,
            source="slack",
            external_id="U123",
            reference_key="slack:U123",
            key_version=1,
            tier=1,
            confidence="verified",
            verified_by="auto",
            first_seen=now,
            last_seen=now,
        )
    )
    scope_b.commit()  # must not raise
