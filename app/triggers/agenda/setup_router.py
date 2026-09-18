"""First-time self-service setup: email a one-time link to a Notion-linked
person's real email address, then redeem that link for their real
agenda_client_secret. Split out of app.triggers.agenda.agenda_router, which had
grown to cover setup, login, agenda orchestration, preferences, goals, and
ledgers all in one ~1300-line file.

issue_setup_link is also called by app.triggers.agenda.agenda_scheduler's Notion
auto-provisioning job (system-initiated, for a freshly auto-created User)
— imported from here rather than duplicated.
"""

import datetime
import logging

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.models import User
from app.delivery.email_deliverer import EmailDeliverer
from app.triggers.agenda.webhook_auth import _open_session, _token_is_valid

logger = logging.getLogger(__name__)

router = APIRouter()


def _find_user_by_email(session: Session, email: str) -> User | None:
    """Case-insensitive lookup by notion_owner_email. REAL BUG FOUND AND
    FIXED (confirmed live): both login_webhook and request_setup_link_
    webhook previously did `User.notion_owner_email == email` — a plain
    case-SENSITIVE match. Neither `mentor link-notion` nor auto-
    provisioning normalizes case on write, and email casing a user types
    at login can legitimately differ from what's stored."""
    return session.execute(
        select(User).where(func.lower(User.notion_owner_email) == email.lower())
    ).scalar_one_or_none()


def issue_setup_link(session: Session, user: User, base_url: str) -> None:
    """Shared by request_setup_link_webhook (human-initiated, via the UI's
    "Request setup link") and agenda_scheduler's Notion auto-provisioning
    job (system-initiated, for a freshly auto-created User — see
    app/ingest/notion_user_provision.py) — same one-time 24h token +
    email-delivered link either way."""
    import secrets as secrets_module

    token = secrets_module.token_urlsafe(32)
    user.setup_token = token
    user.setup_token_expires_at = datetime.datetime.now(datetime.UTC) + datetime.timedelta(
        hours=24
    )
    session.commit()

    setup_url = f"{base_url.rstrip('/')}/ui?setup_token={token}"
    result = EmailDeliverer().send(
        to=user.notion_owner_email,
        subject="Set up your Mentor Agent access",
        body=(
            "Click this link to get your access secret (expires in "
            f"24 hours):\n\n{setup_url}\n\nIf you didn't request this, "
            "you can ignore this email."
        ),
    )
    if not result.get("sent"):
        # Deliberately a log, not this instead of raising — without
        # checking it here, a broken send (expired OAuth grant, MCP not
        # configured, etc.) was completely invisible: the caller's own
        # route always replies "ok" regardless (see
        # request_setup_link_webhook's docstring on why), so this log
        # line is the only signal anyone gets.
        logger.warning(
            "issue_setup_link: email send failed for user_id=%s reason=%s",
            user.id,
            result.get("reason"),
        )


@router.post("/webhooks/request-setup-link")
async def request_setup_link_webhook(request: Request) -> JSONResponse:
    """First-time self-service setup: sends a one-time link to a Notion-
    linked person's real email (EmailDeliverer, app/delivery/
    email_deliverer.py, backed by Gmail's real sendEmail tool)."""
    settings = get_settings()
    if not _token_is_valid(settings, request):
        return JSONResponse(status_code=401, content={"status": "invalid token"})

    generic_ok = {"status": "ok"}

    try:
        body = await request.json()
        email = body["email"]

        session = _open_session()
        try:
            user = _find_user_by_email(session, email)
            if user is None:
                # Always "ok" regardless of whether the email matched a
                # user — same reasoning as login_webhook's own invalid-
                # credentials response: don't let this endpoint be used to
                # enumerate which emails are registered.
                return JSONResponse(status_code=200, content=generic_ok)

            issue_setup_link(session, user, str(request.base_url))
        finally:
            session.close()
    except (ValueError, KeyError) as exc:
        return JSONResponse(
            status_code=400, content={"status": "rejected", "reason": str(exc)}
        )

    return JSONResponse(status_code=200, content=generic_ok)


@router.post("/webhooks/complete-setup")
async def complete_setup_webhook(request: Request) -> JSONResponse:
    """Redeems a one-time setup token (see request_setup_link_webhook)
    for a real agenda_client_secret — the same credential `mentor link-
    agenda-client` generates, just delivered via the email link instead
    of an admin's terminal. Single-use: setup_token is cleared the
    moment it's redeemed (or found expired), same "only shown in
    plaintext once" posture as link-agenda-client's own CLI output."""
    settings = get_settings()
    if not _token_is_valid(settings, request):
        return JSONResponse(status_code=401, content={"status": "invalid token"})

    import secrets as secrets_module

    invalid = {
        "status": "rejected",
        "reason": "setup link is invalid or has expired",
    }

    try:
        body = await request.json()
        token = body["token"]

        session = _open_session()
        try:
            user = session.execute(
                select(User).where(User.setup_token == token)
            ).scalar_one_or_none()
            if user is None:
                return JSONResponse(status_code=400, content=invalid)

            expired = (
                user.setup_token_expires_at is None
                or user.setup_token_expires_at < datetime.datetime.now(datetime.UTC)
            )
            if expired:
                user.setup_token = None
                user.setup_token_expires_at = None
                session.commit()
                return JSONResponse(status_code=400, content=invalid)

            secret = secrets_module.token_urlsafe(32)
            user.agenda_client_secret = secret
            user.setup_token = None
            user.setup_token_expires_at = None
            result = {
                "status": "ok",
                "email": user.notion_owner_email,
                "display_name": user.notion_display_name,
                "secret": secret,
            }
            session.commit()
        finally:
            session.close()
    except (ValueError, KeyError) as exc:
        return JSONResponse(
            status_code=400, content={"status": "rejected", "reason": str(exc)}
        )

    return JSONResponse(status_code=200, content=result)
