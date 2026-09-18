import hmac
import logging
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from app.core.clock import SystemClock
from app.core.config import get_settings
from app.core.db import get_engine, get_session_factory
from app.core.models import User
from app.core.scope import OwnerScope
from app.sub_agents.friday_review.sub_agents.gather.agent import gather_friday_review_context
from app.sub_agents.mentor_advisor.agent import build_mentor_advisor_agent, generate_mentor_advice
from app.triggers.webhooks.webhook_signature import verify_shared_token
logger = logging.getLogger(__name__)
router = APIRouter()
_ACTING_USER_SECRET_REJECTION = {
    "status": "rejected",
    "reason": "acting user secret missing or invalid",
}

def _open_session():
    return get_session_factory(get_engine(get_settings().database_url))()

def _token_is_valid(settings, request: Request) -> bool:
    return bool(settings.agenda_webhook_token) and verify_shared_token(
        settings.agenda_webhook_token, request.headers.get("X-Agenda-Token")
    )

def _acting_user_secret_is_valid(session, owner_user_id: str, request: Request) -> bool:
    provided = request.headers.get("X-Acting-User-Secret")
    if provided is None:
        return False
    user = session.get(User, owner_user_id)
    if user is None or user.agenda_client_secret is None:
        return False
    return hmac.compare_digest(user.agenda_client_secret, provided)

@router.get("/webhooks/mentor-advice")
async def mentor_advice_webhook(request: Request) -> JSONResponse:
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

        scope = OwnerScope(owner_user_id=owner_user_id, session=session)
        context = gather_friday_review_context(scope, SystemClock())
        try:
            advice = generate_mentor_advice(context, build_mentor_advisor_agent())
        except Exception:
            logger.exception(
                "mentor_advice_webhook: generate_mentor_advice failed for "
                "owner_user_id=%s",
                owner_user_id,
            )
            return JSONResponse(
                status_code=502,
                content={
                    "status": "rejected",
                    "reason": "mentor advice generation failed — try again in a moment",
                },
            )
        return JSONResponse(
            status_code=200,
            content={
                "status": "ok",
                "short_term_tasks": advice.short_term_tasks,
                "long_term_tasks": advice.long_term_tasks,
                "rationale": advice.rationale,
            },
        )
    finally:
        session.close()
