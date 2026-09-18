import datetime
import uuid

from app.core.clock import FrozenClock
from app.core.models import Event
from app.identity import config
from app.identity.models import (
    Identity,
    MergeLog,
    NotSameAs,
    PendingConfirmation,
    Person,
    RosterVersion,
    UnresolvedReference,
)
from app.identity.resolve import (
    attach_ask,
    confirm,
    reject,
    resolve,
    resolve_for_surface,
    unlink,
)
from app.identity.types import RawReference, Resolved, Unattributed, Unconfirmed

NOW = datetime.datetime(2026, 8, 5, 8, 30, tzinfo=datetime.UTC)
LATER = NOW + datetime.timedelta(hours=1)


def _seed_unresolved_reference(
    db_session, scope, person_id, reference_key="slack:handle:sbenali"
):
    db_session.add(RosterVersion(owner_user_id=scope.owner_user_id, version=1))
    scope.add(
        UnresolvedReference(
            id=str(uuid.uuid4()),
            reference_key=reference_key,
            key_version=1,
            source="slack",
            external_id=None,
            handle="sbenali",
            email=None,
            display_name=None,
            candidates=[{"person_id": person_id, "tier": 4, "score": 0.8}],
            best_score=0.8,
            margin=0.2,
            occurrence_count=1,
            distinct_day_count=1,
            last_seen_date=NOW.date(),
            ask_count=0,
            scored_at=NOW,
            roster_version=1,
            status="pending",
            first_seen=NOW,
            last_seen=NOW,
        )
    )
    scope.commit()


def test_confirm_writes_identity_and_backfills_historical_rows(db_session, make_scope):
    scope = make_scope()
    person = Person(
        id=str(uuid.uuid4()),
        canonical_name="Sarah Ben Ali",
        primary_email=None,
        is_self=False,
        is_active=True,
        created_at=NOW,
    )
    scope.add(person)
    _seed_unresolved_reference(db_session, scope, person.id)
    scope.add(
        Event(
            id=str(uuid.uuid4()),
            actor_reference_key="slack:handle:sbenali",
            resolved_person_id=None,
        )
    )
    scope.commit()

    confirm(scope, "slack:handle:sbenali", person.id, FrozenClock(at=LATER))

    identity = (
        db_session.query(Identity)
        .filter_by(
            owner_user_id=scope.owner_user_id, reference_key="slack:handle:sbenali"
        )
        .one()
    )
    assert identity.verified_by == "user_confirmed"
    assert identity.confidence == "verified"
    event = (
        db_session.query(Event)
        .filter_by(
            owner_user_id=scope.owner_user_id,
            actor_reference_key="slack:handle:sbenali",
        )
        .one()
    )
    assert event.resolved_person_id == person.id
    assert (
        db_session.query(UnresolvedReference)
        .filter_by(
            owner_user_id=scope.owner_user_id, reference_key="slack:handle:sbenali"
        )
        .count()
        == 0
    )


def test_confirm_never_backfills_another_owners_event(db_session, make_scope):
    scope_a = make_scope()
    scope_b = make_scope()
    person_a = Person(
        id=str(uuid.uuid4()),
        canonical_name="Sarah Ben Ali",
        primary_email=None,
        is_self=False,
        is_active=True,
        created_at=NOW,
    )
    scope_a.add(person_a)
    _seed_unresolved_reference(db_session, scope_a, person_a.id)
    # owner B happens to have an event with the SAME reference_key text — this
    # must never be touched by owner A's confirm()
    scope_b.add(
        Event(
            id=str(uuid.uuid4()),
            actor_reference_key="slack:handle:sbenali",
            resolved_person_id=None,
        )
    )
    scope_a.commit()
    scope_b.commit()

    confirm(scope_a, "slack:handle:sbenali", person_a.id, FrozenClock(at=LATER))

    owner_b_event = (
        db_session.query(Event).filter_by(owner_user_id=scope_b.owner_user_id).one()
    )
    assert owner_b_event.resolved_person_id is None


def test_confirm_is_idempotent(db_session, make_scope):
    scope = make_scope()
    person = Person(
        id=str(uuid.uuid4()),
        canonical_name="Sarah Ben Ali",
        primary_email=None,
        is_self=False,
        is_active=True,
        created_at=NOW,
    )
    scope.add(person)
    _seed_unresolved_reference(db_session, scope, person.id)
    scope.commit()

    confirm(scope, "slack:handle:sbenali", person.id, FrozenClock(at=LATER))
    confirm(
        scope, "slack:handle:sbenali", person.id, FrozenClock(at=LATER)
    )  # no-op, no error

    assert (
        db_session.query(Identity)
        .filter_by(
            owner_user_id=scope.owner_user_id, reference_key="slack:handle:sbenali"
        )
        .count()
        == 1
    )


def test_reject_persists_not_same_as_and_filters_future_candidates(
    db_session, make_scope
):
    scope = make_scope()
    rejected_person = Person(
        id=str(uuid.uuid4()),
        canonical_name="Wrong Person",
        primary_email=None,
        is_self=False,
        is_active=True,
        created_at=NOW,
    )
    correct_person = Person(
        id=str(uuid.uuid4()),
        canonical_name="Right Person",
        primary_email="right@acme.com",
        is_self=False,
        is_active=True,
        created_at=NOW,
    )
    scope.add(rejected_person)
    scope.add(correct_person)
    _seed_unresolved_reference(db_session, scope, rejected_person.id)
    scope.commit()

    reject(scope, "slack:handle:sbenali", rejected_person.id, FrozenClock(at=LATER))

    assert (
        db_session.query(NotSameAs)
        .filter_by(
            owner_user_id=scope.owner_user_id,
            reference_key="slack:handle:sbenali",
            person_id=rejected_person.id,
        )
        .count()
        == 1
    )
    row = (
        db_session.query(UnresolvedReference)
        .filter_by(
            owner_user_id=scope.owner_user_id, reference_key="slack:handle:sbenali"
        )
        .one()
    )
    assert row.candidates == []
    assert row.best_score is None


def test_confirm_then_unlink_then_reingest_never_relinks(db_session, make_scope):
    scope = make_scope()
    # canonical_name is deliberately chosen so match_handle_heuristic's real
    # prefix-ratio scoring produces an exact 1.0 (tier 4) candidate for
    # handle "sbenali" on re-ingest: normalize_handle("S Benali") joined
    # without spaces is "sbenali", identical to the handle's own stem. A
    # loosely-related name (e.g. "Sarah Ben Ali") does NOT reach the tier-4
    # inclusion floor here (_prefix_ratio("sbenali","sarahbenali") ~= 0.11),
    # so resolve() would return Unattributed on re-ingest regardless of
    # whether the NotSameAs filter is doing anything — that made the
    # assertion below vacuous. A second, unrelated person is added so
    # roster_size satisfies MIN_ROSTER_SIZE and the tier-4 score/margin
    # clear the auto_link gate on their own — so if the NotSameAs filter in
    # resolve() were ever broken, this test would genuinely catch a
    # Resolved(person_id=person.id) coming back.
    person = Person(
        id=str(uuid.uuid4()),
        canonical_name="S Benali",
        primary_email=None,
        is_self=False,
        is_active=True,
        created_at=NOW,
    )
    decoy_person = Person(
        id=str(uuid.uuid4()),
        canonical_name="Wrong Person",
        primary_email=None,
        is_self=False,
        is_active=True,
        created_at=NOW,
    )
    scope.add(person)
    scope.add(decoy_person)
    _seed_unresolved_reference(db_session, scope, person.id)
    scope.add(
        Event(
            id=str(uuid.uuid4()),
            actor_reference_key="slack:handle:sbenali",
            resolved_person_id=None,
        )
    )
    scope.commit()

    confirm(scope, "slack:handle:sbenali", person.id, FrozenClock(at=LATER))
    unlink(
        scope,
        "slack:handle:sbenali",
        reason="wrong person",
        clock=FrozenClock(at=LATER),
    )

    assert (
        db_session.query(Identity)
        .filter_by(
            owner_user_id=scope.owner_user_id, reference_key="slack:handle:sbenali"
        )
        .count()
        == 0
    )
    event = (
        db_session.query(Event)
        .filter_by(
            owner_user_id=scope.owner_user_id,
            actor_reference_key="slack:handle:sbenali",
        )
        .one()
    )
    assert event.resolved_person_id is None
    split_log = (
        db_session.query(MergeLog)
        .filter_by(
            owner_user_id=scope.owner_user_id,
            reference_key="slack:handle:sbenali",
            action="split",
        )
        .one()
    )
    assert split_log.prev_person_id == person.id

    # re-ingest the same reference — the real matchers now DO produce a
    # tier-4 candidate for this person that clears the auto_link gate on
    # its own (see comment above); this must not silently re-link because
    # unlink() recorded a NotSameAs row for (reference_key, person.id).
    ref = RawReference(
        source="slack",
        external_id=None,
        handle="sbenali",
        email=None,
        display_name=None,
    )
    result = resolve(scope, ref, FrozenClock(at=LATER))
    assert not (isinstance(result, Resolved) and result.person_id == person.id)


def test_attach_ask_respects_max_asks_per_candidate(db_session, make_scope):
    scope = make_scope()
    person = Person(
        id=str(uuid.uuid4()),
        canonical_name="Sarah Ben Ali",
        primary_email=None,
        is_self=False,
        is_active=True,
        created_at=NOW,
    )
    scope.add(person)
    _seed_unresolved_reference(db_session, scope, person.id)
    scope.commit()

    first = attach_ask(
        scope,
        "slack:handle:sbenali",
        person.id,
        0.8,
        "card",
        "card-1",
        FrozenClock(at=NOW),
    )
    second = attach_ask(
        scope,
        "slack:handle:sbenali",
        person.id,
        0.8,
        "friday_batch",
        None,
        FrozenClock(at=LATER),
    )

    assert first is not None
    assert second is None  # MAX_ASKS_PER_CANDIDATE == 1, already asked once


def test_resolve_for_surface_returns_unconfirmed_for_fresh_ambiguous_reference(
    db_session, make_scope
):
    scope = make_scope()
    person = Person(
        id=str(uuid.uuid4()),
        canonical_name="Sarah Ben Ali",
        primary_email=None,
        is_self=False,
        is_active=True,
        created_at=NOW,
    )
    scope.add(person)
    _seed_unresolved_reference(db_session, scope, person.id)
    scope.commit()

    result = resolve_for_surface(scope, "slack:handle:sbenali", FrozenClock(at=NOW))

    assert isinstance(result, Unconfirmed)
    assert result.top.person_id == person.id


def test_resolve_for_surface_unknown_key_is_unattributed(db_session, make_scope):
    scope = make_scope()
    db_session.add(RosterVersion(owner_user_id=scope.owner_user_id, version=1))
    db_session.commit()

    result = resolve_for_surface(scope, "slack:handle:nobody", FrozenClock(at=NOW))

    assert isinstance(result, Unattributed)


def _add_pending(scope, reference_key, candidate_person_id):
    pending = PendingConfirmation(
        id=str(uuid.uuid4()),
        reference_key=reference_key,
        candidate_person_id=candidate_person_id,
        candidate_score=0.8,
        surface="card",
        card_ref="card-1",
        status="pending",
        asked_at=NOW,
        answered_at=None,
        expires_at=NOW + datetime.timedelta(days=7),
    )
    scope.add(pending)
    return pending


def test_attach_ask_returns_none_when_a_live_pending_row_already_exists(
    db_session, make_scope, monkeypatch
):
    """C1 regression (card path): attach_ask() only checked ask_count, so a
    second ask for the same (reference_key, candidate_person_id) raised an
    IntegrityError from uq_pending_confirmation_owner_key_candidate instead of
    returning None. MAX_ASKS_PER_CANDIDATE is raised here so ask_count is not
    the thing doing the blocking."""
    monkeypatch.setattr(config, "MAX_ASKS_PER_CANDIDATE", 2)
    scope = make_scope()
    person = Person(
        id=str(uuid.uuid4()),
        canonical_name="Sarah Ben Ali",
        primary_email=None,
        is_self=False,
        is_active=True,
        created_at=NOW,
    )
    scope.add(person)
    _seed_unresolved_reference(db_session, scope, person.id)
    scope.commit()

    first = attach_ask(
        scope,
        "slack:handle:sbenali",
        person.id,
        0.8,
        "card",
        "card-1",
        FrozenClock(at=NOW),
    )
    second = attach_ask(
        scope,
        "slack:handle:sbenali",
        person.id,
        0.8,
        "card",
        "card-2",
        FrozenClock(at=LATER),
    )

    assert first is not None
    assert second is None
    assert (
        db_session.query(PendingConfirmation)
        .filter_by(
            owner_user_id=scope.owner_user_id, reference_key="slack:handle:sbenali"
        )
        .count()
        == 1
    )


def test_confirm_closes_pending_rows_for_candidates_it_did_not_pick(
    db_session, make_scope
):
    """I2 regression: the picker UI surfaces the top candidate but the user may
    pick anyone on the roster. The asked-about candidate's PendingConfirmation
    must not be left status='pending' forever."""
    scope = make_scope()
    asked_about = Person(
        id=str(uuid.uuid4()),
        canonical_name="Asked About",
        primary_email=None,
        is_self=False,
        is_active=True,
        created_at=NOW,
    )
    actually_picked = Person(
        id=str(uuid.uuid4()),
        canonical_name="Actually Picked",
        primary_email=None,
        is_self=False,
        is_active=True,
        created_at=NOW,
    )
    scope.add(asked_about)
    scope.add(actually_picked)
    _seed_unresolved_reference(db_session, scope, asked_about.id)
    _add_pending(scope, "slack:handle:sbenali", asked_about.id)
    scope.commit()

    confirm(scope, "slack:handle:sbenali", actually_picked.id, FrozenClock(at=LATER))

    orphan = (
        db_session.query(PendingConfirmation)
        .filter_by(
            owner_user_id=scope.owner_user_id,
            reference_key="slack:handle:sbenali",
            candidate_person_id=asked_about.id,
        )
        .one()
    )
    assert orphan.status != "pending"
    assert orphan.answered_at is not None


def test_unlink_closes_live_pending_rows_for_the_reference(db_session, make_scope):
    """I2 regression: unlink() did not touch PendingConfirmation at all."""
    scope = make_scope()
    person = Person(
        id=str(uuid.uuid4()),
        canonical_name="Sarah Ben Ali",
        primary_email=None,
        is_self=False,
        is_active=True,
        created_at=NOW,
    )
    other = Person(
        id=str(uuid.uuid4()),
        canonical_name="Other Person",
        primary_email=None,
        is_self=False,
        is_active=True,
        created_at=NOW,
    )
    scope.add(person)
    scope.add(other)
    _seed_unresolved_reference(db_session, scope, person.id)
    scope.commit()

    confirm(scope, "slack:handle:sbenali", person.id, FrozenClock(at=LATER))
    # a stray live ask for a different candidate outlives the confirm/unlink
    _add_pending(scope, "slack:handle:sbenali", other.id)
    scope.commit()

    unlink(
        scope,
        "slack:handle:sbenali",
        reason="wrong person",
        clock=FrozenClock(at=LATER),
    )

    stray = (
        db_session.query(PendingConfirmation)
        .filter_by(
            owner_user_id=scope.owner_user_id,
            reference_key="slack:handle:sbenali",
            candidate_person_id=other.id,
        )
        .one()
    )
    assert stray.status != "pending"


def test_auto_link_closes_the_live_pending_ask_it_answers(db_session, make_scope):
    """Round-2 I1: _write_identity() deleted the UnresolvedReference (the C2
    fix) but left any live PendingConfirmation for it status='pending' forever
    — nothing sweeps expires_at, and a user answering the now-stale card hits
    confirm()'s already_linked early return, so the row never closes. Same
    orphan class I2 fixed, surviving on the auto-link-not-via-confirm path."""
    scope = make_scope()
    person = Person(
        id=str(uuid.uuid4()),
        canonical_name="Sarah Ben Youssef",
        primary_email=None,
        is_self=False,
        is_active=True,
        created_at=NOW,
    )
    other = Person(
        id=str(uuid.uuid4()),
        canonical_name="Other Person",
        primary_email=None,
        is_self=False,
        is_active=True,
        created_at=NOW,
    )
    scope.add(person)
    scope.add(other)
    db_session.add(RosterVersion(owner_user_id=scope.owner_user_id, version=1))
    scope.commit()

    # 1. first ingest lands in the ask band, and a card asks about it
    ref = RawReference(
        source="calendar",
        external_id=None,
        handle=None,
        email=None,
        display_name="Sarah Ben Youssef",
    )
    result = resolve(scope, ref, FrozenClock(at=NOW))
    pending = attach_ask(
        scope,
        result.reference_key,
        person.id,
        0.85,
        "card",
        "card-1",
        FrozenClock(at=NOW),
    )
    assert pending is not None

    # 2. matching-relevant roster change, then re-ingest -> tier-1 auto-link
    person.primary_email = "sarah@acme.com"
    db_session.get(RosterVersion, scope.owner_user_id).version = 2
    scope.commit()
    ref_with_email = RawReference(
        source="calendar",
        external_id=None,
        handle=None,
        email="sarah@acme.com",
        display_name="Sarah Ben Youssef",
    )
    relinked = resolve(scope, ref_with_email, FrozenClock(at=LATER))
    assert relinked.person_id == person.id

    # the ask the auto-link just answered must not still be open
    row = (
        db_session.query(PendingConfirmation)
        .filter_by(
            owner_user_id=scope.owner_user_id, reference_key=result.reference_key
        )
        .one()
    )
    assert row.status == "confirmed"
    assert row.answered_at is not None


def test_resolve_for_surface_stale_refresh_does_not_inflate_volume_counters(
    db_session, make_scope
):
    """I3 regression: resolve_for_surface() is a display path. Its stale-row
    refresh delegated to resolve(), whose _upsert_unresolved bumps
    occurrence_count and (on a new calendar day) distinct_day_count — so
    rendering the same card on three days inflated exactly the signal
    build_batch() ranks by (spec §4.8)."""
    scope = make_scope()
    person = Person(
        id=str(uuid.uuid4()),
        canonical_name="Sarah Ben Youssef",
        primary_email=None,
        is_self=False,
        is_active=True,
        created_at=NOW,
    )
    other = Person(
        id=str(uuid.uuid4()),
        canonical_name="Other Person",
        primary_email=None,
        is_self=False,
        is_active=True,
        created_at=NOW,
    )
    scope.add(person)
    scope.add(other)
    # roster has moved on since this row was scored
    db_session.add(RosterVersion(owner_user_id=scope.owner_user_id, version=2))
    scope.commit()

    key = "calendar:handle:sarah ben youssef"
    scope.add(
        UnresolvedReference(
            id=str(uuid.uuid4()),
            reference_key=key,
            key_version=1,
            source="calendar",
            external_id=None,
            handle=None,
            email=None,
            display_name="Sarah Ben Youssef",
            candidates=[],
            best_score=None,
            margin=None,
            occurrence_count=5,
            distinct_day_count=3,
            last_seen_date=NOW.date(),
            ask_count=0,
            scored_at=NOW,
            roster_version=1,
            status="pending",
            first_seen=NOW,
            last_seen=NOW,
        )
    )
    scope.commit()

    # rendered on a LATER calendar day — the day-rollover branch is what used
    # to bump distinct_day_count
    result = resolve_for_surface(
        scope, key, FrozenClock(at=NOW + datetime.timedelta(days=2))
    )

    row = (
        db_session.query(UnresolvedReference)
        .filter_by(owner_user_id=scope.owner_user_id, reference_key=key)
        .one()
    )
    assert row.occurrence_count == 5  # volume untouched by a display-path read
    assert row.distinct_day_count == 3
    assert row.last_seen_date == NOW.date()
    # ...but the scoring fields ARE refreshed
    assert row.roster_version == 2
    assert row.best_score == config.TIER3_EXACT_NAME_SCORE
    assert row.candidates
    assert isinstance(result, Unconfirmed)
