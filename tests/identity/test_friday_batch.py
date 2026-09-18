import datetime
import uuid

from app.core.clock import FrozenClock
from app.identity.friday_batch import build_batch, commit_batch
from app.identity.models import (
    Identity,
    PendingConfirmation,
    Person,
    RosterVersion,
    UnresolvedReference,
)
from app.identity.resolve import attach_ask, confirm, reject, unlink

NOW = datetime.datetime(2026, 8, 5, 16, 0, tzinfo=datetime.UTC)


def _seed(
    db_session,
    scope,
    reference_key,
    person_id,
    distinct_day_count=1,
    status="pending",
    roster_version=1,
):
    scope.add(
        UnresolvedReference(
            id=str(uuid.uuid4()),
            reference_key=reference_key,
            key_version=1,
            source="slack",
            external_id=None,
            handle=reference_key.split(":")[-1],
            email=None,
            display_name=None,
            candidates=[{"person_id": person_id, "tier": 4, "score": 0.8}],
            best_score=0.8,
            margin=0.2,
            occurrence_count=distinct_day_count,
            distinct_day_count=distinct_day_count,
            last_seen_date=NOW.date(),
            ask_count=0,
            scored_at=NOW,
            roster_version=roster_version,
            status=status,
            first_seen=NOW,
            last_seen=NOW,
        )
    )


def _seed_person(scope):
    person = Person(
        id=str(uuid.uuid4()),
        canonical_name="Sarah Ben Ali",
        primary_email=None,
        is_self=False,
        is_active=True,
        created_at=NOW,
    )
    scope.add(person)
    return person


def test_build_batch_excludes_live_pending_card_ask(db_session, make_scope):
    scope = make_scope()
    db_session.add(RosterVersion(owner_user_id=scope.owner_user_id, version=1))
    for _ in range(2):
        _seed_person(scope)
    person = _seed_person(scope)
    scope.commit()
    _seed(db_session, scope, "slack:handle:a", person.id)
    scope.add(
        PendingConfirmation(
            id=str(uuid.uuid4()),
            reference_key="slack:handle:a",
            candidate_person_id=person.id,
            candidate_score=0.8,
            surface="card",
            card_ref="card-1",
            status="pending",
            asked_at=NOW,
            answered_at=None,
            expires_at=NOW + datetime.timedelta(days=7),
        )
    )
    scope.commit()

    batch = build_batch(scope)

    assert batch == []


def test_build_batch_ranks_by_distinct_day_count(db_session, make_scope):
    scope = make_scope()
    db_session.add(RosterVersion(owner_user_id=scope.owner_user_id, version=1))
    for _ in range(2):
        _seed_person(scope)
    chatty_person = _seed_person(scope)
    quiet_person = _seed_person(scope)
    _seed(
        db_session, scope, "slack:handle:chatty", chatty_person.id, distinct_day_count=1
    )
    _seed(
        db_session, scope, "slack:handle:quiet", quiet_person.id, distinct_day_count=5
    )
    scope.commit()

    batch = build_batch(scope)

    assert batch[0].reference_key == "slack:handle:quiet"


def test_build_batch_never_includes_another_owners_references(db_session, make_scope):
    scope_a = make_scope()
    scope_b = make_scope()
    db_session.add(RosterVersion(owner_user_id=scope_a.owner_user_id, version=1))
    db_session.add(RosterVersion(owner_user_id=scope_b.owner_user_id, version=1))
    for _ in range(2):
        _seed_person(scope_a)
    person_b = _seed_person(scope_b)
    _seed(db_session, scope_b, "slack:handle:only-b", person_b.id)
    scope_a.commit()
    scope_b.commit()

    batch_a = build_batch(scope_a)

    assert batch_a == []


def test_commit_batch_creates_pending_confirmations_and_flips_status(
    db_session, make_scope
):
    scope = make_scope()
    db_session.add(RosterVersion(owner_user_id=scope.owner_user_id, version=1))
    for _ in range(2):
        _seed_person(scope)
    person = _seed_person(scope)
    _seed(db_session, scope, "slack:handle:a", person.id)
    scope.commit()

    batch = build_batch(scope)
    commit_batch(scope, batch, card_ref="friday-2026-08-07", clock=FrozenClock(at=NOW))

    row = (
        db_session.query(UnresolvedReference)
        .filter_by(owner_user_id=scope.owner_user_id, reference_key="slack:handle:a")
        .one()
    )
    assert row.status == "surfaced"
    assert row.ask_count == 1
    confirmation = (
        db_session.query(PendingConfirmation)
        .filter_by(
            owner_user_id=scope.owner_user_id,
            reference_key="slack:handle:a",
            surface="friday_batch",
        )
        .one()
    )
    assert confirmation.card_ref == "friday-2026-08-07"


def test_committed_batch_never_reselected(db_session, make_scope):
    scope = make_scope()
    db_session.add(RosterVersion(owner_user_id=scope.owner_user_id, version=1))
    for _ in range(2):
        _seed_person(scope)
    person = _seed_person(scope)
    _seed(db_session, scope, "slack:handle:a", person.id)
    scope.commit()

    first_batch = build_batch(scope)
    commit_batch(scope, first_batch, card_ref="friday-1", clock=FrozenClock(at=NOW))

    second_batch = build_batch(scope)

    assert second_batch == []


def test_partial_send_failure_leaves_undelivered_rows_pending(db_session, make_scope):
    scope = make_scope()
    db_session.add(RosterVersion(owner_user_id=scope.owner_user_id, version=1))
    for _ in range(2):
        _seed_person(scope)
    person_a = _seed_person(scope)
    person_b = _seed_person(scope)
    _seed(db_session, scope, "slack:handle:a", person_a.id)
    _seed(db_session, scope, "slack:handle:b", person_b.id)
    scope.commit()

    batch = build_batch(scope)
    delivered_only = [item for item in batch if item.reference_key == "slack:handle:a"]
    commit_batch(scope, delivered_only, card_ref="friday-1", clock=FrozenClock(at=NOW))

    undelivered = (
        db_session.query(UnresolvedReference)
        .filter_by(owner_user_id=scope.owner_user_id, reference_key="slack:handle:b")
        .one()
    )
    assert undelivered.status == "pending"


def test_commit_batch_skips_item_that_collided_with_a_card_ask(db_session, make_scope):
    """C1 regression: attach_ask() firing for the same
    (reference_key, candidate_person_id) in the window between build_batch()
    and commit_batch() used to violate uq_pending_confirmation_owner_key_candidate
    and — because the whole batch is one commit — lose the ENTIRE week's batch,
    not just the one colliding item."""
    scope = make_scope()
    db_session.add(RosterVersion(owner_user_id=scope.owner_user_id, version=1))
    for _ in range(2):
        _seed_person(scope)
    person_a = _seed_person(scope)
    person_b = _seed_person(scope)
    _seed(db_session, scope, "slack:handle:a", person_a.id)
    _seed(db_session, scope, "slack:handle:b", person_b.id)
    scope.commit()

    batch = build_batch(scope)
    assert {item.reference_key for item in batch} == {
        "slack:handle:a",
        "slack:handle:b",
    }

    # a card renders slack:handle:a between build_batch and commit_batch
    attach_ask(
        scope,
        "slack:handle:a",
        person_a.id,
        0.8,
        "card",
        "card-1",
        FrozenClock(at=NOW),
    )

    commit_batch(scope, batch, card_ref="friday-1", clock=FrozenClock(at=NOW))

    # the colliding item is skipped, its UnresolvedReference untouched by
    # commit_batch (ask_count still reflects only the card ask)
    row_a = (
        db_session.query(UnresolvedReference)
        .filter_by(owner_user_id=scope.owner_user_id, reference_key="slack:handle:a")
        .one()
    )
    assert row_a.ask_count == 1
    assert (
        db_session.query(PendingConfirmation)
        .filter_by(owner_user_id=scope.owner_user_id, reference_key="slack:handle:a")
        .count()
        == 1
    )
    # ...and the rest of the batch still went out
    row_b = (
        db_session.query(UnresolvedReference)
        .filter_by(owner_user_id=scope.owner_user_id, reference_key="slack:handle:b")
        .one()
    )
    assert row_b.status == "surfaced"
    assert row_b.ask_count == 1


def test_build_batch_excludes_row_stale_against_current_roster_version(
    db_session, make_scope
):
    """I1 regression: reject()/unlink() bump RosterVersion for the WHOLE owner,
    so one reject makes every other pending row's stored best_score/margin
    stale. build_batch() must not ask questions computed from pre-reject
    scores."""
    scope = make_scope()
    db_session.add(RosterVersion(owner_user_id=scope.owner_user_id, version=1))
    for _ in range(2):
        _seed_person(scope)
    person_a = _seed_person(scope)
    person_b = _seed_person(scope)
    _seed(db_session, scope, "slack:handle:a", person_a.id)
    _seed(db_session, scope, "slack:handle:b", person_b.id)
    scope.commit()

    # rejecting ANY reference bumps RosterVersion for the whole owner
    reject(scope, "slack:handle:a", person_a.id, FrozenClock(at=NOW))

    batch = build_batch(scope)

    # slack:handle:b was never touched, but its stored score is now stale
    assert "slack:handle:b" not in {item.reference_key for item in batch}


def test_build_batch_tolerates_multiple_live_pending_rows_for_one_reference(
    db_session, make_scope
):
    """I4 regression: the live-pending exclusion query filters only on
    reference_key + status, but the partial unique index is on
    (owner, reference_key, candidate_person_id) — two live pending rows for
    one reference_key are legal and must not raise MultipleResultsFound."""
    scope = make_scope()
    db_session.add(RosterVersion(owner_user_id=scope.owner_user_id, version=1))
    for _ in range(2):
        _seed_person(scope)
    person_a = _seed_person(scope)
    person_b = _seed_person(scope)
    _seed(db_session, scope, "slack:handle:a", person_a.id)
    scope.commit()
    for candidate in (person_a, person_b):
        scope.add(
            PendingConfirmation(
                id=str(uuid.uuid4()),
                reference_key="slack:handle:a",
                candidate_person_id=candidate.id,
                candidate_score=0.8,
                surface="card",
                card_ref="card-1",
                status="pending",
                asked_at=NOW,
                answered_at=None,
                expires_at=NOW + datetime.timedelta(days=7),
            )
        )
    scope.commit()

    assert build_batch(scope) == []


def test_build_batch_skips_reference_already_linked_by_an_identity(
    db_session, make_scope
):
    """C2 defense-in-depth: a reference_key with a live Identity must never be
    surfaced as a Friday-batch ask, even if a stale UnresolvedReference row
    for it somehow survives."""
    scope = make_scope()
    db_session.add(RosterVersion(owner_user_id=scope.owner_user_id, version=1))
    for _ in range(2):
        _seed_person(scope)
    person = _seed_person(scope)
    _seed(db_session, scope, "slack:handle:a", person.id)
    scope.commit()
    scope.add(
        Identity(
            id=str(uuid.uuid4()),
            person_id=person.id,
            source="slack",
            external_id=None,
            reference_key="slack:handle:a",
            key_version=1,
            tier=1,
            confidence="verified",
            verified_by="auto",
            handle="a",
            email=None,
            display_name=None,
            provenance={},
            first_seen=NOW,
            last_seen=NOW,
        )
    )
    scope.commit()

    assert build_batch(scope) == []


def test_commit_batch_still_records_items_that_went_stale_after_build(
    db_session, make_scope
):
    """Round-2 I2: putting I1's staleness check in the SHARED eligibility gate
    meant commit_batch re-ran it per item — and since reject()/unlink() bump
    RosterVersion for the WHOLE owner, one reject in the build->commit window
    staled EVERY item at once and silently dropped the entire batch. Worse
    than the original C1 crash: the card has already been composed and sent
    from `batch`, so the user sees asks with zero backing rows.

    Staleness is a build-time question ("is this still worth asking?"), not a
    commit-time one ("can this ask legally be recorded?")."""
    scope = make_scope()
    db_session.add(RosterVersion(owner_user_id=scope.owner_user_id, version=1))
    for _ in range(2):
        _seed_person(scope)
    people = [_seed_person(scope) for _ in range(3)]
    unrelated_person = _seed_person(scope)
    keys = ["slack:handle:a", "slack:handle:b", "slack:handle:c"]
    for key, person in zip(keys, people, strict=True):
        _seed(db_session, scope, key, person.id)
    scope.commit()

    batch = build_batch(scope)
    assert len(batch) == 3

    # the user answers an unrelated earlier card while this batch is in
    # flight — bumping RosterVersion for the whole owner
    reject(scope, "slack:handle:unrelated", unrelated_person.id, FrozenClock(at=NOW))

    commit_batch(scope, batch, card_ref="friday-1", clock=FrozenClock(at=NOW))

    for key in keys:
        row = (
            db_session.query(UnresolvedReference)
            .filter_by(owner_user_id=scope.owner_user_id, reference_key=key)
            .one()
        )
        assert row.status == "surfaced", f"{key} was silently dropped"
        assert row.ask_count == 1
        assert (
            db_session.query(PendingConfirmation)
            .filter_by(owner_user_id=scope.owner_user_id, reference_key=key)
            .count()
            == 1
        )


def test_orphaned_pending_row_never_blocks_a_reference_forever(db_session, make_scope):
    """I2 regression: confirm() used to close only the PendingConfirmation
    matching the chosen candidate. Picking a DIFFERENT roster person from the
    picker left the asked-about candidate's row pending forever, permanently
    blocking that reference_key from any future Friday batch."""
    scope = make_scope()
    db_session.add(RosterVersion(owner_user_id=scope.owner_user_id, version=1))
    for _ in range(2):
        _seed_person(scope)
    asked_about = _seed_person(scope)
    actually_picked = _seed_person(scope)
    later_candidate = _seed_person(scope)
    _seed(db_session, scope, "slack:handle:a", asked_about.id)
    scope.commit()
    scope.add(
        PendingConfirmation(
            id=str(uuid.uuid4()),
            reference_key="slack:handle:a",
            candidate_person_id=asked_about.id,
            candidate_score=0.8,
            surface="card",
            card_ref="card-1",
            status="pending",
            asked_at=NOW,
            answered_at=None,
            expires_at=NOW + datetime.timedelta(days=7),
        )
    )
    scope.commit()

    # the user picks someone the system never asked about
    confirm(scope, "slack:handle:a", actually_picked.id, FrozenClock(at=NOW))
    unlink(scope, "slack:handle:a", reason="wrong person", clock=FrozenClock(at=NOW))

    # re-ingest lands the reference back in the ask band, scored against the
    # roster version unlink() just bumped
    current_version = db_session.get(RosterVersion, scope.owner_user_id).version
    _seed(
        db_session,
        scope,
        "slack:handle:a",
        later_candidate.id,
        roster_version=current_version,
    )
    scope.commit()

    batch = build_batch(scope)

    assert [item.reference_key for item in batch] == ["slack:handle:a"]
