"""Deferred-ask batching for the Friday review (design spec §4.8). build_batch
is read-only; commit_batch is called by L7 only after the batch actually
sends. Both take scope: OwnerScope first — build_batch is invoked once per
owner, at that owner's Friday-review time, never across owners."""

import datetime
import uuid
from dataclasses import dataclass

from app.core.clock import Clock
from app.core.scope import OwnerScope
from app.identity import config, matchers
from app.identity.models import Identity, PendingConfirmation, UnresolvedReference
from app.identity.resolve import has_live_pending_ask
from app.identity.roster import load_snapshot
from app.identity.types import MatchCandidate


@dataclass(frozen=True)
class FridayBatchItem:
    reference_key: str
    handle: str | None
    top: MatchCandidate


def _is_askable(scope: OwnerScope, row: UnresolvedReference, roster) -> bool:
    """BUILD-TIME question: "is this still worth asking about?" — scoring
    judgement only. Used ONLY by build_batch.

    Deliberately NOT re-checked by commit_batch. Staleness in particular must
    never gate an item that has already been selected AND shown: RosterVersion
    is bumped for the WHOLE owner by reject()/unlink() (spec §4.6), so one
    unrelated reject in the build->commit window would stale every item in the
    batch at once and silently drop all of them — after the card carrying
    those asks had already been composed and sent."""
    if not row.candidates or row.best_score is None or row.margin is None:
        return False
    # Stale score: skip rather than re-resolve inline — build_batch is a
    # read-only path (see module docstring), and the row gets freshly scored
    # the next time ingest or resolve_for_surface touches it, which is how
    # resolve_for_surface's own staleness check works.
    if row.roster_version < roster.roster_version:
        return False
    return (
        matchers.gate(row.best_score, row.margin, roster_size=len(roster.people))
        == "ask"
    )


def _can_record_ask(scope: OwnerScope, row: UnresolvedReference) -> bool:
    """COMMIT-TIME question: "can this ask legally be recorded?" — INSERT
    invariants only, no scoring judgement. Used by build_batch (as half of
    full eligibility) and re-run per item by commit_batch, because attach_ask()
    can fire for the same (reference_key, candidate) in the window between the
    two calls. Since the whole batch is one commit, an unguarded INSERT that
    trips uq_pending_confirmation_owner_key_candidate would lose the entire
    week's batch, not just the one colliding item.

    Every check here is about whether the row can carry another ask at all, so
    all of them stay true-or-false independent of how the roster has moved."""
    if row.ask_count >= config.MAX_ASKS_PER_CANDIDATE:
        return False
    # Already resolved: an Identity for this reference_key means the question
    # has been answered, so never ask it again. Defense-in-depth against any
    # auto-link path that forgets to clean up its UnresolvedReference row
    # (_write_identity does, but this keeps a future one from re-opening the
    # gap and asking the user to identify someone already verified-linked).
    already_linked = scope.session.execute(
        scope.query(Identity).where(Identity.reference_key == row.reference_key)
    ).scalar_one_or_none()
    if already_linked is not None:
        return False
    # No surface filter here on purpose: PendingConfirmation has a unique index
    # on (owner_user_id, reference_key, candidate_person_id) WHERE
    # status = 'pending' (models.py), so a live pending ask on ANY surface (not
    # just "card") would collide if we tried to create a second one here.
    # Excluding on reference_key + status alone is what keeps commit_batch's
    # INSERT from violating that constraint.
    return not has_live_pending_ask(scope, row.reference_key)


def build_batch(
    scope: OwnerScope, limit: int = config.FRIDAY_BATCH_CAP
) -> list[FridayBatchItem]:
    roster = load_snapshot(scope)
    rows = (
        scope.session.execute(
            scope.query(UnresolvedReference).where(
                UnresolvedReference.status == "pending"
            )
        )
        .scalars()
        .all()
    )

    eligible = [
        row
        for row in rows
        if _is_askable(scope, row, roster) and _can_record_ask(scope, row)
    ]

    eligible.sort(key=lambda r: r.distinct_day_count, reverse=True)
    return [
        FridayBatchItem(
            reference_key=r.reference_key,
            handle=r.handle,
            top=MatchCandidate(
                person_id=r.candidates[0]["person_id"],
                tier=r.candidates[0]["tier"],
                score=r.candidates[0]["score"],
            ),
        )
        for r in eligible[:limit]
    ]


def commit_batch(
    scope: OwnerScope, batch: list[FridayBatchItem], card_ref: str, clock: Clock
) -> None:
    now = clock.now()
    for item in batch:
        row = scope.session.execute(
            scope.query(UnresolvedReference).where(
                UnresolvedReference.reference_key == item.reference_key
            )
        ).scalar_one_or_none()
        if row is None:
            continue  # resolved/removed since build_batch ran — skip safely
        # Only the INSERT invariants are re-checked, never _is_askable: these
        # items were already selected and already shown to the user, so the
        # question is "can this be recorded?", not "would we pick it again?".
        if not _can_record_ask(scope, row):
            # became unrecordable since build_batch ran (most often: a card
            # rendered the same reference and attach_ask() took the ask).
            # Skip this ONE item, leaving its UnresolvedReference untouched —
            # never let it abort the whole batch's single commit.
            continue
        scope.add(
            PendingConfirmation(
                id=str(uuid.uuid4()),
                reference_key=item.reference_key,
                candidate_person_id=item.top.person_id,
                candidate_score=item.top.score,
                surface="friday_batch",
                card_ref=card_ref,
                status="pending",
                asked_at=now,
                answered_at=None,
                expires_at=now
                + datetime.timedelta(days=config.PENDING_CONFIRMATION_TTL_DAYS),
            )
        )
        row.ask_count += 1
        row.status = "surfaced"
    scope.commit()
