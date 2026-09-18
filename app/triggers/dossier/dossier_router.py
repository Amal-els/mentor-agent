from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from app.core.config import get_settings
from app.core.db import get_engine, get_session_factory
from app.core.models import DossierDelivery
from app.dossier_payload import build_dossier_a2ui_payload
from app.triggers.agenda.webhook_auth import _acting_user_secret_is_valid, _token_is_valid

router = APIRouter()

def _open_session():
    return get_session_factory(get_engine(get_settings().database_url))()

@router.get("/webhooks/dossier-payload")
async def dossier_payload_webhook(request: Request) -> JSONResponse:
    settings = get_settings()
    if not _token_is_valid(settings, request):
        return JSONResponse(status_code=401, content={"status": "invalid token"})
    owner_user_id = request.query_params.get("owner_user_id")
    if owner_user_id is None:
        return JSONResponse(status_code=400, content={"status": "owner_user_id required"})
    session = _open_session()
    try:
        if not _acting_user_secret_is_valid(session, owner_user_id, request):
            return JSONResponse(status_code=401, content={"status": "invalid acting user secret"})
        rows = (
            session.query(DossierDelivery)
            .filter(DossierDelivery.owner_user_id == owner_user_id)
            .order_by(DossierDelivery.created_at.desc())
            .limit(50)
            .all()
        )
        deliveries = [
            {
                "id": r.id,
                "event_external_id": r.event_external_id,
                "sent_at": r.sent_at.isoformat() if r.sent_at else None,
                "who": r.who_summary,
                "why_now": r.why_now,
            }
            for r in rows
        ]
        payload = build_dossier_a2ui_payload(deliveries)
        return JSONResponse(status_code=200, content=payload)
    finally:
        session.close()
