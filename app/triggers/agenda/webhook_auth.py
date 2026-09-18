"""Shared auth/session helpers for the `/webhooks/*` routers.

Extracted from app.triggers.agenda.agenda_router (where every one of these
originated and where several other routers — dossier_router,
google_oauth_router, graph_router — were already importing them from,
while checklist_router had instead copy-pasted its own duplicate copies).
agenda_router.py itself was never the right home for shared auth: it's one
of many feature routers, not an infrastructure module, and every new
router added to app/triggers/ that needed these had to either import from
an unrelated feature file or duplicate them. This module is that home.

Two-layer auth, present on every route below:
  1. `X-Agenda-Token` — a single shared secret (settings.agenda_webhook_token)
     proving the request came from this app's own frontend/scheduler, not
     an arbitrary caller. Checked by _token_is_valid.
  2. `X-Acting-User-Secret` — a per-user secret (User.agenda_client_secret,
     issued via the setup-link flow) proving the request is acting *as*
     the specific owner_user_id/acting_user_id it claims. Checked by
     _acting_user_secret_is_valid.
Both are required on nearly every route; the shared token alone is never
enough to act as a specific user.
"""

import hmac

from fastapi import Request
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.db import get_engine, get_session_factory
from app.core.models import User
from app.triggers.webhooks.webhook_signature import verify_shared_token

_ACTING_USER_SECRET_REJECTION = {
    "status": "rejected",
    "reason": "acting user secret missing or invalid",
}


def _open_session() -> Session:
    engine = get_engine(get_settings().database_url)
    return get_session_factory(engine)()


def _token_is_valid(settings, request: Request) -> bool:
    return bool(settings.agenda_webhook_token) and verify_shared_token(
        settings.agenda_webhook_token, request.headers.get("X-Agenda-Token")
    )


def _acting_user_secret_is_valid(
    session: Session, acting_user_id: str, request: Request
) -> bool:
    provided = request.headers.get("X-Acting-User-Secret")
    if provided is None:
        return False
    user = session.get(User, acting_user_id)
    if user is None or user.agenda_client_secret is None:
        return False
    return hmac.compare_digest(user.agenda_client_secret, provided)
