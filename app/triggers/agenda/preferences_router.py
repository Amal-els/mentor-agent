"""Per-user schedule preferences (timezone, pulse fire time, late-cutoff
time). Split out of app.triggers.agenda.agenda_router — see that module's
history."""

import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.core.config import get_settings
from app.core.models import User
from app.triggers.agenda.webhook_auth import (
    _ACTING_USER_SECRET_REJECTION,
    _acting_user_secret_is_valid,
    _open_session,
    _token_is_valid,
)

router = APIRouter()


@router.post("/webhooks/update-preferences")
async def update_preferences_webhook(request: Request) -> JSONResponse:
    settings = get_settings()
    if not _token_is_valid(settings, request):
        return JSONResponse(status_code=401, content={"status": "invalid token"})

    try:
        body = await request.json()
        acting_user_id = body["acting_user_id"]

        session = _open_session()
        try:
            if not _acting_user_secret_is_valid(session, acting_user_id, request):
                return JSONResponse(
                    status_code=401, content=_ACTING_USER_SECRET_REJECTION
                )

            user = session.get(User, acting_user_id)
            if user is None:
                return JSONResponse(
                    status_code=400,
                    content={"status": "rejected", "reason": "no such user"},
                )

            if "tz" in body:
                tz_value = body["tz"]
                try:
                    ZoneInfo(tz_value)
                except (ZoneInfoNotFoundError, ValueError):
                    return JSONResponse(
                        status_code=400,
                        content={
                            "status": "rejected",
                            "reason": f"unrecognized timezone: {tz_value!r}",
                        },
                    )
                user.tz = tz_value

            for field in ("pulse_fire_time_local", "late_cutoff_local"):
                if field not in body:
                    continue
                try:
                    parsed_time = datetime.time.fromisoformat(body[field])
                except ValueError:
                    return JSONResponse(
                        status_code=400,
                        content={
                            "status": "rejected",
                            "reason": f"{field} must be HH:MM (24h), got {body[field]!r}",
                        },
                    )
                setattr(user, field, parsed_time)

            session.commit()
            result = {
                "status": "ok",
                "tz": user.tz,
                "pulse_fire_time_local": user.pulse_fire_time_local.isoformat(
                    timespec="minutes"
                ),
                "late_cutoff_local": user.late_cutoff_local.isoformat(
                    timespec="minutes"
                ),
            }
        finally:
            session.close()
    except (ValueError, KeyError) as exc:
        return JSONResponse(
            status_code=400, content={"status": "rejected", "reason": str(exc)}
        )

    return JSONResponse(status_code=200, content=result)
