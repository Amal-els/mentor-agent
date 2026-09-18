import logging
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from app.core.clock import SystemClock
from app.core.config import get_settings
from app.core.scope import OwnerScope
from app.salience.pulse.checklist import (
    ChecklistItemView,
    ChecklistView,
    build_checklist,
    find_checklist_item,
    record_completion,
)
from app.sub_agents.checklist.agent import generate_supportive_response
from app.triggers.agenda.webhook_auth import (
    _ACTING_USER_SECRET_REJECTION,
    _acting_user_secret_is_valid,
    _open_session,
    _token_is_valid,
)

logger = logging.getLogger(__name__)

router = APIRouter()

def _item_payload(item: ChecklistItemView) -> dict:
    return {
        "item_id": item.item_id,
        "item_type": item.item_type,
        "title": item.title,
        "url": item.url,
        "detail": item.detail,
        "source": item.status,  # "pending" | "manual" | "auto" — the /ui label, not WorkItem/Message's own `source` column
        "completed_at": item.completed_at.isoformat() if item.completed_at else None,
        "ai_response": item.ai_response,
    }

def _day_event_payload(event) -> dict:
    return {
        "item_id": event.item_id,
        "title": event.title,
        "starts_at": event.starts_at.isoformat(),
        "url": event.url,
        "dossier_id": event.dossier_id,
    }

def _checklist_payload(view: ChecklistView) -> dict:
    return {
        "pending": [_item_payload(i) for i in view.pending],
        "resolved": [_item_payload(i) for i in view.resolved],
        "stats": {"done": view.done_count, "total": view.total_count},
        "day": [_day_event_payload(e) for e in view.day],
    }

@router.get("/webhooks/checklist")
async def checklist_webhook(request: Request) -> JSONResponse:
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
        view = build_checklist(scope, SystemClock())
        return JSONResponse(status_code=200, content=_checklist_payload(view))
    finally:
        session.close()

@router.post("/webhooks/checklist/complete")
async def checklist_complete_webhook(request: Request) -> JSONResponse:
    settings = get_settings()
    if not _token_is_valid(settings, request):
        return JSONResponse(status_code=401, content={"status": "invalid token"})
    try:
        body = await request.json()
        owner_user_id = body["owner_user_id"]
        item_type = body["item_type"]
        item_id = body["item_id"]
    except (KeyError, ValueError) as exc:
        return JSONResponse(
            status_code=400,
            content={
                "status": "rejected",
                "reason": f"owner_user_id, item_type, item_id required ({exc})",
            },
        )
    session = _open_session()
    try:
        if not _acting_user_secret_is_valid(session, owner_user_id, request):
            return JSONResponse(status_code=401, content=_ACTING_USER_SECRET_REJECTION)
        scope = OwnerScope(owner_user_id=owner_user_id, session=session)
        clock = SystemClock()
        item = find_checklist_item(scope, item_type, item_id)
        if item is None:
            return JSONResponse(
                status_code=404,
                content={"status": "rejected", "reason": "item not found"},
            )
        pre_view = build_checklist(scope, clock)
        done_after = pre_view.done_count + 1
        total = pre_view.total_count
        ai_response = generate_supportive_response(item.title, done_after, total)
        record_completion(scope, clock, item_type, item.source_key, ai_response)
        return JSONResponse(
            status_code=200,
            content={
                "status": "ok",
                "ai_response": ai_response,
                "stats": {"done": done_after, "total": total},
            },
        )
    finally:
        session.close()
