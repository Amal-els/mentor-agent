"""A user's own goals/OKRs. Split out of app.triggers.agenda.agenda_router — see
that module's history."""

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.core.config import get_settings
from app.core.models import Goal
from app.core.scope import OwnerScope
from app.triggers.agenda.webhook_auth import (
    _ACTING_USER_SECRET_REJECTION,
    _acting_user_secret_is_valid,
    _open_session,
    _token_is_valid,
)

router = APIRouter()


def _goal_to_payload(goal: Goal) -> dict:
    return {
        "id": goal.id,
        "external_id": goal.external_id,
        "title": goal.title,
        "goal_type": goal.goal_type,
        "status": goal.status,
        "quarter": goal.quarter,
        "progress": goal.progress,
        "current_value": goal.current_value,
        "target_value": goal.target_value,
        "parent_external_id": goal.parent_external_id,
    }


@router.get("/webhooks/goals")
async def goals_webhook(request: Request) -> JSONResponse:
    settings = get_settings()
    if not _token_is_valid(settings, request):
        return JSONResponse(status_code=401, content={"status": "invalid token"})

    try:
        report_user_id = request.query_params["report_user_id"]
        acting_user_id = request.query_params["acting_user_id"]

        session = _open_session()
        try:
            if not _acting_user_secret_is_valid(session, acting_user_id, request):
                return JSONResponse(
                    status_code=401, content=_ACTING_USER_SECRET_REJECTION
                )

            if acting_user_id != report_user_id:
                return JSONResponse(
                    status_code=403,
                    content={
                        "status": "rejected",
                        "reason": "goals/OKRs are private — cannot view another user's",
                    },
                )

            owner_scope = OwnerScope(owner_user_id=report_user_id, session=session)
            rows = (
                session.execute(
                    owner_scope.query(Goal).where(
                        Goal.goal_type.in_(["objective", "key_result", "career_goal"])
                    )
                )
                .scalars()
                .all()
            )
            goals = [_goal_to_payload(row) for row in rows]
        finally:
            session.close()
    except (ValueError, KeyError) as exc:
        return JSONResponse(
            status_code=400, content={"status": "rejected", "reason": str(exc)}
        )

    return JSONResponse(status_code=200, content={"status": "ok", "goals": goals})
