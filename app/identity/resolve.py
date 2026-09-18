"""The only module in app/identity with side effects (design spec §6). Owns
resolve(), resolve_for_surface(), confirm(), reject(), unlink(), attach_ask().
Every function takes scope: OwnerScope as its first argument (spec §11.3) —
no bare Session, ever."""

import datetime
import logging
import uuid
from typing import Literal

from sqlalchemy import update

from app.core.clock import Clock
from app.core.models import Event, Message, WorkItem
from app.core.scope import OwnerScope
from app.identity import config, matchers
from app.identity.models import (
    Identity,
    MergeLog,
    NotSameAs,
    PendingConfirmation,
    RosterVersion,
    UnresolvedReference,
)
from app.identity.normalize import build_reference_key, safe_log_key
from app.identity.roster import load_snapshot
from app.identity.types import (
    MatchCandidate,
    RawReference,
    Resolution,
    Resolved,
    Unattributed,
    Unconfirmed,
)

logger = logging.getLogger(__name__)


def _run_matcher(matcher, ref, roster, key):
    """Runs one matcher, catching and logging any exception rather than
    letting a malformed input abort the whole tier ladder (design spec §7).
    A matcher that raises is treated as 'did not fire'. Never logs raw
    email/handle content — only the hashed reference_key."""
    try:
        return matcher(ref, roster)
    except Exception:
        logger.exception(
            "matcher %s raised for reference_key=%s",
            matcher.__name__,
            safe_log_key(key),
        )
        return None


def has_live_pending_ask(
    scope: OwnerScope, reference_key: str, candidate_person_id: str | None = None
) -> bool:
    """True if a status='pending' PendingConfirmation exists for this
    reference_key (optionally narrowed to one candidate). Deliberately uses
    .first() rather than .scalar_one_or_none(): the partial unique index is on
    (owner_user_id, reference_key, candidate_person_id) WHERE status='pending'
    (models.py), so MORE THAN ONE live row per reference_key is legal and an
    existence check must tolerate it rather than raise MultipleResultsFound."""
    stmt = scope.query(PendingConfirmation).where(
        PendingConfirmation.reference_key == reference_key,
        PendingConfirmation.status == "pending",
    )
    if candidate_person_id is not None:
        stmt = stmt.where(
            PendingConfirmation.candidate_person_id == candidate_person_id
        )
    return scope.session.execute(stmt).scalars().first() is not None


def _close_live_pending_asks(
    scope: OwnerScope,
    reference_key: str,
    now: datetime.datetime,
    confirmed_person_id: str | None = None,
) -> None:
    """Closes EVERY live pending ask for a reference_key, not just the one
    matching the answer. The picker UI surfaces the top candidate but the user
    can pick anyone on the roster, so a non-matching row would otherwise stay
    'pending' forever with nothing to sweep it — and because build_batch()'s
    live-pending exclusion is keyed on reference_key alone (it has to be, see
    the unique index above), one orphan permanently blocks that reference from
    every future Friday batch."""
    rows = (
        scope.session.execute(
            scope.query(PendingConfirmation).where(
                PendingConfirmation.reference_key == reference_key,
                PendingConfirmation.status == "pending",
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        row.status = (
            "confirmed"
            if confirmed_person_id is not None
            and row.candidate_person_id == confirmed_person_id
            else "superseded"
        )
        row.answered_at = now


def resolve(scope: OwnerScope, ref: RawReference, clock: Clock) -> Resolution:
    return _resolve(scope, ref, clock, rescore_only=False)


def _resolve(
    scope: OwnerScope, ref: RawReference, clock: Clock, rescore_only: bool
) -> Resolution:
    """rescore_only=True is the display-path variant (see resolve_for_surface):
    it refreshes an existing UnresolvedReference's scoring fields but must not
    touch its volume counters."""
    key = build_reference_key(ref.source, ref.external_id, ref.handle, ref.display_name)

    cached = scope.session.execute(
        scope.query(Identity).where(Identity.reference_key == key)
    ).scalar_one_or_none()
    if cached is not None:
        return Resolved(
            person_id=cached.person_id, tier=cached.tier, confidence=cached.confidence
        )

    roster = load_snapshot(scope)

    for tier_matcher in (
        matchers.match_idp,
        matchers.match_primary_email,
        matchers.match_alias_email,
    ):
        hit = _run_matcher(tier_matcher, ref, roster, key)
        if hit is not None:
            _write_identity(
                scope, hit.person_id, ref, key, hit.tier, "verified", "auto", clock
            )
            return Resolved(
                person_id=hit.person_id, tier=hit.tier, confidence="verified"
            )

    candidates: list[MatchCandidate] = []
    exact = _run_matcher(matchers.match_exact_name, ref, roster, key)
    if exact is not None:
        candidates.append(exact)
    candidates.extend(
        _run_matcher(matchers.match_handle_heuristic, ref, roster, key) or []
    )
    candidates.extend(_run_matcher(matchers.match_fuzzy_name, ref, roster, key) or [])
    candidates = [c for c in candidates if (key, c.person_id) not in roster.not_same_as]
    candidates = matchers.apply_context_priors(candidates, ref, roster)
    candidates.sort(key=lambda c: c.score, reverse=True)

    if not candidates:
        _upsert_unresolved(
            scope, ref, key, [], None, None, roster.roster_version, clock, rescore_only
        )
        return Unattributed(reference_key=key, raw_handle=ref.handle)

    top = candidates[0]
    second_score = candidates[1].score if len(candidates) > 1 else 0.0
    margin = round(top.score - second_score, 4)
    outcome = matchers.gate(top.score, margin, roster_size=len(roster.people))

    if outcome == "auto_link":
        _write_identity(
            scope, top.person_id, ref, key, top.tier, "inferred", "auto", clock
        )
        return Resolved(person_id=top.person_id, tier=top.tier, confidence="inferred")

    candidate_payload = [
        {"person_id": c.person_id, "tier": c.tier, "score": c.score} for c in candidates
    ]
    _upsert_unresolved(
        scope,
        ref,
        key,
        candidate_payload,
        top.score,
        margin,
        roster.roster_version,
        clock,
        rescore_only,
    )

    if outcome == "ask":
        return Unconfirmed(reference_key=key, top=top, margin=margin)
    return Unattributed(reference_key=key, raw_handle=ref.handle)


def _write_identity(
    scope: OwnerScope,
    person_id: str,
    ref: RawReference,
    key: str,
    tier: int,
    confidence: str,
    verified_by: str,
    clock: Clock,
) -> None:
    now = clock.now()
    scope.add(
        Identity(
            id=str(uuid.uuid4()),
            person_id=person_id,
            source=ref.source,
            external_id=ref.external_id,
            reference_key=key,
            key_version=config.KEY_VERSION,
            tier=tier,
            confidence=confidence,
            verified_by=verified_by,
            handle=ref.handle,
            email=ref.email,
            display_name=ref.display_name,
            provenance={"tier": tier},
            first_seen=now,
            last_seen=now,
        )
    )
    scope.add(
        MergeLog(
            id=str(uuid.uuid4()),
            person_id=person_id,
            reference_key=key,
            action="auto_link",
            prev_person_id=None,
            payload={"tier": tier, "confidence": confidence},
            tier=tier,
            confidence=confidence,
            actor="system",
            reason=f"tier {tier} match",
            created_at=now,
        )
    )
    # An auto-link resolves the reference, so any UnresolvedReference row left
    # over from an earlier ask-band pass must go — exactly the cleanup
    # confirm() does. Without it, a reference that first landed in the ask band
    # and later auto-linked after a matching-relevant roster change would sit
    # in Identity AND UnresolvedReference at once (the no-dual-membership
    # invariant, tests/identity/test_invariants.py), and build_batch() would go
    # on asking the user to identify someone already verified-linked.
    # Idempotent "delete if present" — absent is the normal tier 0-2 case.
    stale_unresolved = scope.session.execute(
        scope.query(UnresolvedReference).where(UnresolvedReference.reference_key == key)
    ).scalar_one_or_none()
    if stale_unresolved is not None:
        scope.session.delete(stale_unresolved)
    # ...and close any ask still open against it, exactly as confirm() does.
    # An auto-link answers the question the ask was posing, but it never goes
    # through confirm() — and nothing sweeps expires_at — so a live row left
    # here would stay "pending" forever: a user answering the now-stale card
    # hits confirm()'s already_linked early return, which closes nothing.
    _close_live_pending_asks(scope, key, now, confirmed_person_id=person_id)
    # No RosterVersion bump: an Identity write for an already-known Person
    # is not matching-relevant (spec §4.6), and never touches another
    # owner's RosterVersion row regardless (spec §11.2).
    scope.commit()


def _upsert_unresolved(
    scope: OwnerScope,
    ref: RawReference,
    key: str,
    candidates: list[dict],
    best_score: float | None,
    margin: float | None,
    roster_version: int,
    clock: Clock,
    rescore_only: bool = False,
) -> None:
    now = clock.now()
    today = now.date()
    existing = scope.session.execute(
        scope.query(UnresolvedReference).where(UnresolvedReference.reference_key == key)
    ).scalar_one_or_none()

    if existing is None:
        scope.add(
            UnresolvedReference(
                id=str(uuid.uuid4()),
                reference_key=key,
                key_version=config.KEY_VERSION,
                source=ref.source,
                external_id=ref.external_id,
                handle=ref.handle,
                email=ref.email,
                display_name=ref.display_name,
                candidates=candidates,
                best_score=best_score,
                margin=margin,
                occurrence_count=1,
                distinct_day_count=1,
                last_seen_date=today,
                ask_count=0,
                scored_at=now,
                roster_version=roster_version,
                status="pending",
                first_seen=now,
                last_seen=now,
            )
        )
    else:
        # Scoring-relevant fields: refreshed on every pass, ingest or display.
        existing.candidates = candidates
        existing.best_score = best_score
        existing.margin = margin
        existing.scored_at = now
        existing.roster_version = roster_version
        # Volume-tracking fields: only a genuine ingest event may move these.
        # distinct_day_count is the Friday-batch ranking signal precisely
        # because it means "came up again", not "was shown again" (spec §4.8) —
        # letting a card render bump it would conflate the two.
        if not rescore_only:
            existing.occurrence_count += 1
            if existing.last_seen_date != today:
                existing.distinct_day_count += 1
                existing.last_seen_date = today
            existing.last_seen = now

    scope.commit()


_ATTRIBUTED_TABLES = (Event, WorkItem, Message)


def _bump_roster_version(scope: OwnerScope) -> None:
    row = scope.session.get(RosterVersion, scope.owner_user_id)
    if row is None:
        scope.add(RosterVersion(version=1))
    else:
        row.version += 1


def confirm(
    scope: OwnerScope, reference_key: str, person_id: str, clock: Clock
) -> None:
    already_linked = scope.session.execute(
        scope.query(Identity).where(Identity.reference_key == reference_key)
    ).scalar_one_or_none()
    if already_linked is not None:
        return  # idempotent

    unresolved = scope.session.execute(
        scope.query(UnresolvedReference).where(
            UnresolvedReference.reference_key == reference_key
        )
    ).scalar_one_or_none()
    if unresolved is None:
        return  # idempotent: nothing left to confirm

    now = clock.now()
    tier = next(
        (c["tier"] for c in unresolved.candidates if c["person_id"] == person_id), 5
    )

    scope.add(
        Identity(
            id=str(uuid.uuid4()),
            person_id=person_id,
            source=unresolved.source,
            external_id=unresolved.external_id,
            reference_key=reference_key,
            key_version=config.KEY_VERSION,
            tier=tier,
            confidence="verified",
            verified_by="user_confirmed",
            handle=unresolved.handle,
            email=unresolved.email,
            display_name=unresolved.display_name,
            provenance={"source": "user_confirmed"},
            first_seen=now,
            last_seen=now,
        )
    )
    scope.add(
        MergeLog(
            id=str(uuid.uuid4()),
            person_id=person_id,
            reference_key=reference_key,
            action="user_confirm",
            prev_person_id=None,
            payload={"candidates": unresolved.candidates},
            tier=tier,
            confidence="verified",
            actor="user",
            reason="user confirmed identity",
            created_at=now,
        )
    )

    # Closes the ask for the confirmed candidate AND supersedes any live ask
    # for a candidate the user did not pick — the reference is resolved either
    # way, so no ask about it may survive.
    _close_live_pending_asks(scope, reference_key, now, confirmed_person_id=person_id)

    # owner_user_id filter here is load-bearing: actor_reference_key is only
    # unique within one owner (spec §11.2) — omitting it would backfill
    # another owner's row that happens to share the same reference_key text.
    for table in _ATTRIBUTED_TABLES:
        scope.session.execute(
            update(table)
            .where(
                table.owner_user_id == scope.owner_user_id,
                table.actor_reference_key == reference_key,
            )
            .values(resolved_person_id=person_id)
        )

    scope.session.delete(unresolved)
    # No RosterVersion bump: linking to an already-existing Person doesn't
    # change what other references are scored against (spec §4.6).
    scope.commit()


def reject(scope: OwnerScope, reference_key: str, person_id: str, clock: Clock) -> None:
    now = clock.now()
    scope.add(
        NotSameAs(
            id=str(uuid.uuid4()),
            reference_key=reference_key,
            person_id=person_id,
            rejected_at=now,
            actor="user",
        )
    )
    _bump_roster_version(scope)

    unresolved = scope.session.execute(
        scope.query(UnresolvedReference).where(
            UnresolvedReference.reference_key == reference_key
        )
    ).scalar_one_or_none()
    if unresolved is not None:
        remaining = [c for c in unresolved.candidates if c["person_id"] != person_id]
        unresolved.candidates = remaining
        if remaining:
            top, second = remaining[0], (remaining[1] if len(remaining) > 1 else None)
            unresolved.best_score = top["score"]
            unresolved.margin = round(
                top["score"] - (second["score"] if second else 0.0), 4
            )
        else:
            unresolved.best_score = None
            unresolved.margin = None

    pending = scope.session.execute(
        scope.query(PendingConfirmation).where(
            PendingConfirmation.reference_key == reference_key,
            PendingConfirmation.candidate_person_id == person_id,
            PendingConfirmation.status == "pending",
        )
    ).scalar_one_or_none()
    if pending is not None:
        pending.status = "rejected"
        pending.answered_at = now

    scope.add(
        MergeLog(
            id=str(uuid.uuid4()),
            person_id=person_id,
            reference_key=reference_key,
            action="user_reject",
            prev_person_id=None,
            payload={"rejected_person_id": person_id},
            tier=None,
            confidence=None,
            actor="user",
            reason="user rejected candidate",
            created_at=now,
        )
    )
    scope.commit()


def unlink(scope: OwnerScope, reference_key: str, reason: str, clock: Clock) -> None:
    identity = scope.session.execute(
        scope.query(Identity).where(Identity.reference_key == reference_key)
    ).scalar_one_or_none()
    if identity is None:
        return  # idempotent: nothing linked

    now = clock.now()
    person_id = identity.person_id
    tier, confidence = identity.tier, identity.confidence
    scope.session.delete(identity)

    for table in _ATTRIBUTED_TABLES:
        scope.session.execute(
            update(table)
            .where(
                table.owner_user_id == scope.owner_user_id,
                table.actor_reference_key == reference_key,
            )
            .values(resolved_person_id=None)
        )

    scope.add(
        MergeLog(
            id=str(uuid.uuid4()),
            person_id=person_id,
            reference_key=reference_key,
            action="split",
            prev_person_id=person_id,
            payload={
                "unlinked_from": person_id,
                "tier": tier,
                "confidence": confidence,
            },
            tier=tier,
            confidence=confidence,
            actor="user",
            reason=reason,
            created_at=now,
        )
    )
    scope.add(
        NotSameAs(
            id=str(uuid.uuid4()),
            reference_key=reference_key,
            person_id=person_id,
            rejected_at=now,
            actor="user",
        )
    )
    # Any ask still open against the reference we just unlinked is dead: the
    # link it was asking about no longer exists. Leaving it 'pending' would
    # block this reference_key from every future Friday batch forever, even
    # after a fresh UnresolvedReference row is created for it on re-ingest.
    _close_live_pending_asks(scope, reference_key, now)
    _bump_roster_version(scope)
    scope.commit()


def resolve_for_surface(
    scope: OwnerScope, reference_key: str, clock: Clock
) -> Resolution:
    identity = scope.session.execute(
        scope.query(Identity).where(Identity.reference_key == reference_key)
    ).scalar_one_or_none()
    if identity is not None:
        return Resolved(
            person_id=identity.person_id,
            tier=identity.tier,
            confidence=identity.confidence,
        )

    unresolved = scope.session.execute(
        scope.query(UnresolvedReference).where(
            UnresolvedReference.reference_key == reference_key
        )
    ).scalar_one_or_none()
    if unresolved is None:
        return Unattributed(reference_key=reference_key, raw_handle=None)

    roster = load_snapshot(scope)
    if unresolved.roster_version < roster.roster_version:
        ref = RawReference(
            source=unresolved.source,
            external_id=unresolved.external_id,
            handle=unresolved.handle,
            email=unresolved.email,
            display_name=unresolved.display_name,
        )
        # rescore_only: this is a display path (a card rendering), not an
        # ingest event. It must refresh the stale score for gating purposes
        # without inflating occurrence_count/distinct_day_count — the Friday
        # batch ranks by "seen again", never by "shown again" (spec §4.8).
        return _resolve(scope, ref, clock, rescore_only=True)

    if (
        not unresolved.candidates
        or unresolved.best_score is None
        or unresolved.margin is None
    ):
        return Unattributed(reference_key=reference_key, raw_handle=unresolved.handle)

    outcome = matchers.gate(
        unresolved.best_score, unresolved.margin, roster_size=len(roster.people)
    )
    top_dict = unresolved.candidates[0]
    top = MatchCandidate(
        person_id=top_dict["person_id"], tier=top_dict["tier"], score=top_dict["score"]
    )
    if outcome == "ask":
        return Unconfirmed(
            reference_key=reference_key, top=top, margin=unresolved.margin
        )
    return Unattributed(reference_key=reference_key, raw_handle=unresolved.handle)


def attach_ask(
    scope: OwnerScope,
    reference_key: str,
    candidate_person_id: str,
    candidate_score: float,
    surface: Literal["card", "friday_batch"],
    card_ref: str | None,
    clock: Clock,
) -> PendingConfirmation | None:
    unresolved = scope.session.execute(
        scope.query(UnresolvedReference).where(
            UnresolvedReference.reference_key == reference_key
        )
    ).scalar_one_or_none()
    if unresolved is None or unresolved.ask_count >= config.MAX_ASKS_PER_CANDIDATE:
        return None
    # ask_count alone is not enough: it only bounds how many times we've EVER
    # asked, not whether an ask for this exact (reference_key, candidate) is
    # still open. Inserting a second live row would violate
    # uq_pending_confirmation_owner_key_candidate — return None (already
    # asked) rather than raising an IntegrityError at the caller.
    if has_live_pending_ask(scope, reference_key, candidate_person_id):
        return None

    now = clock.now()
    pending = PendingConfirmation(
        id=str(uuid.uuid4()),
        reference_key=reference_key,
        candidate_person_id=candidate_person_id,
        candidate_score=candidate_score,
        surface=surface,
        card_ref=card_ref,
        status="pending",
        asked_at=now,
        answered_at=None,
        expires_at=now + datetime.timedelta(days=config.PENDING_CONFIRMATION_TTL_DAYS),
    )
    scope.add(pending)
    unresolved.ask_count += 1
    unresolved.status = "surfaced"
    scope.commit()
    return pending
