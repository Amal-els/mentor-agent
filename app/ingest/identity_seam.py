"""The only door from L2/L3 ingestion into identity resolution (AGENT.md's
resolve_actor() scope note in docs/plans/morning-pulse.md). Deliberately
narrower than app.identity.resolve.resolve(): exact provider ID (via the
Identity cache) or exact primary email only — no fuzzy matching, no
confirmation asks, no UnresolvedReference bookkeeping. Read-only: writing
Identity rows stays app.identity.resolve.resolve()'s job.

Exception: resolve_slack_actor() — when an opaque Slack user ID (e.g.
"U08AMALID") arrives from a real-time message event, it cannot be matched
by email (Slack's message payload never includes one). This function makes
a single users.info call to fetch the sender's email, then runs the full
resolve() pipeline so the Identity row is written and cached, meaning every
subsequent message from the same sender is free (hits the Identity cache
on the first branch of resolve_actor())."""

import logging
import os

from app.core.clock import Clock
from app.core.scope import OwnerScope
from app.identity import matchers
from app.identity.models import Identity
from app.identity.roster import load_snapshot
from app.identity.types import RawReference, Resolved

logger = logging.getLogger(__name__)


def resolve_actor(scope: OwnerScope, actor_reference_key: str) -> str | None:
    cached = scope.session.execute(
        scope.query(Identity).where(Identity.reference_key == actor_reference_key)
    ).scalar_one_or_none()
    if cached is not None:
        return cached.person_id

    source, _, rest = actor_reference_key.partition(":")
    if "@" not in rest:
        return None

    roster = load_snapshot(scope)
    ref = RawReference(
        source=source, external_id=rest, handle=None, email=rest, display_name=None
    )
    hit = matchers.match_primary_email(ref, roster)
    return hit.person_id if hit is not None else None


def resolve_slack_actor(
    scope: OwnerScope,
    slack_user_id: str,
    clock: Clock,
) -> str | None:
    """Resolve an opaque Slack user ID (e.g. 'U08AMALID') to a Person by:
    1. Checking the Identity cache (free — already resolved before).
    2. Calling Slack's users.info API to fetch the sender's email.
    3. Running the full resolve() pipeline against the roster using that email.
       If the email matches a known Person, an Identity row is written so
       future messages from this Slack user hit step 1 instead of step 2.
    4. Falling back silently if the Slack API is unavailable or returns no email.

    This is the ONLY place in identity_seam that writes Identity rows — it
    delegates entirely to app.identity.resolve.resolve() to do so, preserving
    the invariant that only that module has side-effects on Identity."""
    reference_key = f"slack:{slack_user_id}"

    # Step 1: identity cache
    cached = scope.session.execute(
        scope.query(Identity).where(Identity.reference_key == reference_key)
    ).scalar_one_or_none()
    if cached is not None:
        return cached.person_id

    # Step 2: Slack users.info API
    email: str | None = None
    display_name: str | None = None
    try:
        from slack_sdk import WebClient
        bot_token = os.environ.get("SLACK_BOT_TOKEN")
        if not bot_token:
            return None
        result = WebClient(bot_token).users_info(user=slack_user_id)
        if isinstance(result, dict):
            user_obj = result.get("user") or {}
            profile = user_obj.get("profile") or {}
            email = profile.get("email")
            display_name = (
                profile.get("display_name")
                or profile.get("real_name")
                or user_obj.get("real_name")
            )
    except Exception:
        logger.warning(
            "resolve_slack_actor: users.info failed for slack_user_id=%s",
            slack_user_id,
        )
        return None

    if not email:
        logger.debug(
            "resolve_slack_actor: no email in users.info response for slack_user_id=%s",
            slack_user_id,
        )
        return None

    # Step 3: full resolve() pipeline — writes Identity row on match
    from app.identity.resolve import resolve
    ref = RawReference(
        source="slack",
        external_id=slack_user_id,
        handle=None,
        email=email,
        display_name=display_name,
    )
    resolution = resolve(scope, ref, clock)
    if isinstance(resolution, Resolved):
        logger.info(
            "resolve_slack_actor: linked slack_user_id=%s -> person_id=%s via email=%s",
            slack_user_id,
            resolution.person_id,
            # never log raw email — just the domain for debugging
            "@" + email.split("@")[-1] if "@" in email else "<no-domain>",
        )
        return resolution.person_id

    return None
