"""L3 normalization turning a live Notion Objectives Owner/Manager
directory scan (LiveNotionPairClient.fetch_directory) into User rows —
the auto-onboarding counterpart to the manual `mentor link-notion` CLI
step, plus (via revoke_departed_notion_users) auto-offboarding when
someone leaves the workspace.

Notion's people-type property is a picker bound to the real workspace
member/guest list (confirmed live: API-get-users returns the identical
id/email/name shape) — anyone discovered via fetch_directory() is
therefore already, by construction, a real workspace member. There is
no separate "is this really a member" check needed at provisioning
time. The reverse direction needs its own check, though: someone who
WAS a member and got removed doesn't show up anywhere in a fresh
Objectives scan to prove they left — revocation needs an independent
live API-get-users poll (LiveNotionPairClient.fetch_active_member_ids),
not just a diff against the narrower Objectives-derived directory.
"""

import datetime
import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.clock import Clock
from app.core.models import User

logger = logging.getLogger(__name__)

# Circuit breaker for revoke_departed_notion_users (see its own docstring
# for the real incident this responds to): if fetch_active_member_ids
# ever returns a wrong-but-not-None member set — an empty set from a
# misparsed error response was the actual bug found live, but any bad
# input has the same shape of damage — a single pass could silently
# clear EVERY currently-linked real user's agenda_client_secret at once.
# Revoking a small handful of people in one tick is normal (someone left
# the team); revoking most of a workspace in one tick almost certainly
# means the input was bad, not that a workspace mass-exited between two
# scheduler ticks 10 minutes apart. Below this candidate count, the
# fraction check is skipped entirely — a 2-person workspace where one
# person leaves is a real, legitimate 50% revocation and must not be
# blocked by a threshold tuned for larger workspaces.
_REVOCATION_CIRCUIT_BREAKER_MIN_CANDIDATES = 5
_REVOCATION_CIRCUIT_BREAKER_MAX_FRACTION = 0.5


def auto_provision_notion_users(
    session: Session,
    directory: list[dict],
    clock: Clock,
    default_tz: str = "UTC",
    default_pulse_fire_time_local: datetime.time = datetime.time(8, 30),
    default_late_cutoff_local: datetime.time = datetime.time(21, 0),
) -> list[User]:
    """directory: LiveNotionPairClient.fetch_directory()'s output — one
    row per distinct person seen in Objectives' Owner/Manager fields on
    this scan, each {"notion_person_id", "email", "display_name"}.

    Idempotent: matches existing rows by notion_person_id first, falling
    back to notion_owner_email (covers users linked manually via `mentor
    link-notion` before notion_person_id existed — backfills it onto
    their existing row rather than creating a duplicate).

    tz/pulse_fire_time_local/late_cutoff_local aren't knowable from
    Notion at all — every genuinely new row gets the given org-wide
    defaults rather than inventing a per-person guess.

    Returns only the NEWLY CREATED rows (not ones merely matched/
    backfilled) — the caller (agenda_scheduler) uses this to know
    exactly who should get a first-time onboarding email, exactly
    once."""
    created: list[User] = []
    for entry in directory:
        person_id = entry["notion_person_id"]
        email = entry["email"]
        display_name = entry.get("display_name")

        existing = session.execute(
            select(User).where(User.notion_person_id == person_id)
        ).scalar_one_or_none()
        if existing is not None:
            continue

        by_email = session.execute(
            select(User).where(User.notion_owner_email == email)
        ).scalar_one_or_none()
        if by_email is not None:
            by_email.notion_person_id = person_id
            continue

        user = User(
            id=f"notion-{person_id}",
            created_at=clock.now(),
            tz=default_tz,
            pulse_fire_time_local=default_pulse_fire_time_local,
            late_cutoff_local=default_late_cutoff_local,
            notion_person_id=person_id,
            notion_owner_email=email,
            notion_display_name=display_name,
        )
        session.add(user)
        session.flush()
        created.append(user)

    session.commit()
    return created


def revoke_departed_notion_users(
    session: Session, current_member_ids: set[str], clock: Clock
) -> list[User]:
    """current_member_ids: a full live API-get-users snapshot (see
    LiveNotionPairClient.fetch_active_member_ids), not the narrower
    Objectives-derived directory — someone can be removed from the
    workspace without ever having been an Objectives Owner/Manager again
    to prove it.

    Any previously-linked User (notion_person_id set, agenda_client_
    secret still active) whose id is no longer in current_member_ids has
    left/been removed: their secret is cleared and notion_access_
    revoked_at is stamped. Clearing the secret IS the enforcement — every
    X-Acting-User-Secret check and POST /webhooks/login in
    agenda_router.py already fail closed the moment agenda_client_secret
    is None. The row itself is kept (not deleted) so their ledger/agenda
    history stays intact; only future access is cut off.

    Circuit breaker (added after a real incident): if current_member_ids
    is wrong in a way that isn't caught upstream — confirmed live, an
    earlier bug in fetch_active_member_ids silently turned a Notion API
    error response into an empty set instead of None, and this function,
    given that bad-but-not-None input, correctly-per-its-own-logic
    revoked THREE real, currently-active linked users' agenda_client_
    secret in one pass — a single call that would revoke most of an
    already-linked workspace at once is refused (logged, returns [])
    rather than executed, below _REVOCATION_CIRCUIT_BREAKER_MIN_
    CANDIDATES this is skipped entirely (a tiny workspace losing half
    its people in one tick is a real, legitimate event, not a signal of
    bad input)."""
    candidates = (
        session.execute(
            select(User).where(
                User.notion_person_id.is_not(None),
                User.agenda_client_secret.is_not(None),
            )
        )
        .scalars()
        .all()
    )
    to_revoke = [u for u in candidates if u.notion_person_id not in current_member_ids]

    if (
        len(candidates) >= _REVOCATION_CIRCUIT_BREAKER_MIN_CANDIDATES
        and len(to_revoke) / len(candidates) > _REVOCATION_CIRCUIT_BREAKER_MAX_FRACTION
    ):
        logger.warning(
            "revoke_departed_notion_users: refusing to revoke %s of %s "
            "currently-linked users in one pass (over the %.0f%% circuit-"
            "breaker threshold) — this looks like bad input (e.g. an "
            "empty/incomplete member list), not a real mass-departure; "
            "no changes made this cycle",
            len(to_revoke),
            len(candidates),
            _REVOCATION_CIRCUIT_BREAKER_MAX_FRACTION * 100,
        )
        return []

    for user in to_revoke:
        user.agenda_client_secret = None
        user.notion_access_revoked_at = clock.now()

    session.commit()
    return to_revoke
