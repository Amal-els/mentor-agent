import datetime
import uuid

from app.core.clock import FrozenClock
from app.identity.models import Identity, MergeLog, RosterVersion, UnresolvedReference
from app.identity.resolve import resolve
from app.identity.types import RawReference, Resolved, Unattributed, Unconfirmed

NOW = datetime.datetime(2026, 8, 5, 8, 30, tzinfo=datetime.UTC)


def _seed_person(db_session, scope, **overrides):
    from app.identity.models import Person

    person = Person(
        id=overrides.get("id", str(uuid.uuid4())),
        canonical_name=overrides.get("canonical_name", "Sarah Ben Youssef"),
        primary_email=overrides.get("primary_email"),
        is_self=overrides.get("is_self", False),
        is_active=overrides.get("is_active", True),
        created_at=NOW,
    )
    scope.add(person)
    db_session.add(RosterVersion(owner_user_id=scope.owner_user_id, version=1))
    scope.commit()
    return person


def test_tier1_email_match_writes_identity_and_merge_log(db_session, make_scope):
    scope = make_scope()
    person = _seed_person(db_session, scope, primary_email="sarah@acme.com")
    ref = RawReference(
        source="slack",
        external_id="U1",
        handle="sarah.dev",
        email="sarah@acme.com",
        display_name=None,
    )

    result = resolve(scope, ref, FrozenClock(at=NOW))

    assert isinstance(result, Resolved)
    assert result.person_id == person.id
    assert result.confidence == "verified"
    identity = (
        db_session.query(Identity)
        .filter_by(owner_user_id=scope.owner_user_id, reference_key="slack:U1")
        .one()
    )
    assert identity.person_id == person.id
    log = (
        db_session.query(MergeLog)
        .filter_by(owner_user_id=scope.owner_user_id, reference_key="slack:U1")
        .one()
    )
    assert log.action == "auto_link"


def test_second_ingest_of_same_reference_is_a_cache_hit_no_rescan(
    db_session, make_scope
):
    scope = make_scope()
    person = _seed_person(db_session, scope, primary_email="sarah@acme.com")
    ref = RawReference(
        source="slack",
        external_id="U1",
        handle="sarah.dev",
        email="sarah@acme.com",
        display_name=None,
    )
    resolve(scope, ref, FrozenClock(at=NOW))
    before = (
        db_session.query(Identity).filter_by(owner_user_id=scope.owner_user_id).count()
    )

    result = resolve(scope, ref, FrozenClock(at=NOW))

    assert isinstance(result, Resolved)
    assert result.person_id == person.id
    assert (
        db_session.query(Identity).filter_by(owner_user_id=scope.owner_user_id).count()
        == before
    )  # no new row


def test_no_match_produces_unattributed_and_upserts_unresolved(db_session, make_scope):
    scope = make_scope()
    _seed_person(
        db_session, scope, canonical_name="Someone Else", primary_email="x@acme.com"
    )
    ref = RawReference(
        source="slack",
        external_id="U2",
        handle="totallyunrelated",
        email=None,
        display_name=None,
    )

    result = resolve(scope, ref, FrozenClock(at=NOW))

    assert isinstance(result, Unattributed)
    row = (
        db_session.query(UnresolvedReference)
        .filter_by(owner_user_id=scope.owner_user_id, reference_key="slack:U2")
        .one()
    )
    assert row.status == "pending"


def test_ambiguous_exact_name_is_unconfirmed_not_resolved(db_session, make_scope):
    scope = make_scope()
    _seed_person(db_session, scope, canonical_name="Sarah Ben Youssef")
    ref = RawReference(
        source="calendar",
        external_id=None,
        handle=None,
        email=None,
        display_name="Sarah Ben Youssef",
    )

    result = resolve(scope, ref, FrozenClock(at=NOW))

    # tier 3 (exact name) never auto-links — see spec §4.3
    assert isinstance(result, Unconfirmed)


def test_tier_0_2_write_does_not_bump_roster_version(db_session, make_scope):
    scope = make_scope()
    _seed_person(db_session, scope, primary_email="sarah@acme.com")
    version_before = db_session.get(RosterVersion, scope.owner_user_id).version
    ref = RawReference(
        source="slack",
        external_id="U1",
        handle="sarah.dev",
        email="sarah@acme.com",
        display_name=None,
    )

    resolve(scope, ref, FrozenClock(at=NOW))

    assert db_session.get(RosterVersion, scope.owner_user_id).version == version_before


def test_a_raising_matcher_is_treated_as_did_not_fire(
    db_session, make_scope, monkeypatch
):
    scope = make_scope()
    _seed_person(db_session, scope, primary_email="sarah@acme.com")

    def _boom(ref, roster):
        raise ValueError("malformed input")

    monkeypatch.setattr("app.identity.matchers.match_exact_name", _boom)
    ref = RawReference(
        source="calendar",
        external_id=None,
        handle=None,
        email=None,
        display_name="Sarah Ben Youssef",
    )

    result = resolve(scope, ref, FrozenClock(at=NOW))

    # tier 3 raised and was skipped; no other tier can match this reference,
    # so it degrades to Unattributed rather than propagating the exception
    assert isinstance(result, Unattributed)


def test_resolve_never_matches_across_owners(db_session, make_scope):
    scope_a = make_scope()
    scope_b = make_scope()
    _seed_person(db_session, scope_a, primary_email="sarah@acme.com")
    # scope_b has an empty roster — the same email must NOT resolve against
    # scope_a's Person, because roster.load_snapshot(scope_b) never sees it
    ref = RawReference(
        source="slack",
        external_id="U1",
        handle="sarah.dev",
        email="sarah@acme.com",
        display_name=None,
    )

    result = resolve(scope_b, ref, FrozenClock(at=NOW))

    assert not isinstance(result, Resolved)
