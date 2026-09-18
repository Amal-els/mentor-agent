"""Login and the manager↔report relationships a logged-in user can act
on. Split out of app.triggers.agenda.agenda_router (see that module's history —
setup_router.py, preferences_router.py, goals_router.py, ledgers_router.py
were split out of it the same pass, for the same reason: one file had
grown to cover every unrelated feature area behind /webhooks/*)."""

import hmac

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.agenda.models import Pair
from app.core.config import get_settings
from app.core.models import User
from app.triggers.agenda.setup_router import _find_user_by_email
from app.triggers.agenda.webhook_auth import (
    _ACTING_USER_SECRET_REJECTION,
    _acting_user_secret_is_valid,
    _open_session,
    _token_is_valid,
)
from sqlalchemy import select

router = APIRouter()


def _compute_managed_reports(session, user_id: str) -> list[dict]:
    manager_pairs = (
        session.execute(
            select(Pair).where(Pair.manager_user_id == user_id, Pair.ended_at.is_(None))
        )
        .scalars()
        .all()
    )
    return [
        {
            "report_user_id": pair.report_user_id,
            "display_name": report.notion_display_name if report else pair.report_user_id,
        }
        for pair in manager_pairs
        for report in [session.get(User, pair.report_user_id)]
    ]


@router.get("/webhooks/managed-reports")
async def managed_reports_webhook(request: Request) -> JSONResponse:
    settings = get_settings()
    if not _token_is_valid(settings, request):
        return JSONResponse(status_code=401, content={"status": "invalid token"})

    owner_user_id = request.query_params.get("owner_user_id")
    if owner_user_id is None:
        return JSONResponse(
            status_code=400,
            content={"status": "rejected", "reason": "owner_user_id required"},
        )

    session = _open_session()
    try:
        if not _acting_user_secret_is_valid(session, owner_user_id, request):
            return JSONResponse(status_code=401, content=_ACTING_USER_SECRET_REJECTION)

        managed_reports = _compute_managed_reports(session, owner_user_id)
        return JSONResponse(
            status_code=200,
            content={"status": "ok", "managed_reports": managed_reports},
        )
    finally:
        session.close()


@router.post("/webhooks/login")
async def login_webhook(request: Request) -> JSONResponse:
    settings = get_settings()
    if not _token_is_valid(settings, request):
        return JSONResponse(status_code=401, content={"status": "invalid token"})

    invalid_credentials = {
        "status": "rejected",
        "reason": "invalid email or secret",
    }

    try:
        body = await request.json()
        email = body["email"]
        secret = body["secret"]

        session = _open_session()
        try:
            user = _find_user_by_email(session, email)
            if (
                user is None
                or user.agenda_client_secret is None
                or not hmac.compare_digest(user.agenda_client_secret, secret)
            ):
                return JSONResponse(status_code=401, content=invalid_credentials)

            managed_reports = _compute_managed_reports(session, user.id)

            result = {
                "status": "ok",
                "user_id": user.id,
                "display_name": user.notion_display_name or email,
                "managed_reports": managed_reports,
            }
        finally:
            session.close()
    except (ValueError, KeyError) as exc:
        return JSONResponse(
            status_code=400, content={"status": "rejected", "reason": str(exc)}
        )

    return JSONResponse(status_code=200, content=result)
