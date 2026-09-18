"""Rolling 1-on-1 agenda orchestration: signals, triggers, and the live
agenda payload. Split out of what used to be one ~1300-line file covering
setup, login, agenda orchestration, preferences, goals, and ledgers all
together — see app.triggers.agenda.setup_router / auth_router / preferences_router
/ goals_router / ledgers_router / webhook_auth for the rest, and any of
their module docstrings for the split's full reasoning.
"""

import collections
import datetime
import logging
import uuid

from fastapi import APIRouter, BackgroundTasks, Request
from fastapi.responses import JSONResponse
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from app.agenda.payload import build_a2ui_payload
from app.agenda.scope import resolve_pair_scope
from app.agenda.store import get_agenda
from app.core.clock import SystemClock
from app.core.config import get_settings
from app.core.guardrails import GUARDRAIL_PLUGINS
from app.core.scope import OwnerScope
from app.sub_agents.agenda.agent import RollingAgendaOrchestrator
from app.sub_agents.agenda.sub_agents.deliver.agent import _item_to_dict
from app.triggers.agenda.agenda_signal_handler import handle_agenda_signal
from app.triggers.agenda.webhook_auth import (
    _ACTING_USER_SECRET_REJECTION,
    _acting_user_secret_is_valid,
    _open_session,
    _token_is_valid,
)

logger = logging.getLogger(__name__)

router = APIRouter()

_AGENDA_TRIGGER_APP_NAME = "agenda_trigger"

_MAX_TRACKED_MEETING_END_RUNS = 200
_meeting_end_status: dict[str, dict] = collections.OrderedDict()


def _record_meeting_end_status(
    meeting_id: str, acting_user_id: str, status: str, detail: str | None
) -> None:
    if len(_meeting_end_status) >= _MAX_TRACKED_MEETING_END_RUNS:
        _meeting_end_status.pop(next(iter(_meeting_end_status)))
    _meeting_end_status[meeting_id] = {
        "acting_user_id": acting_user_id,
        "status": status,
        "detail": detail,
        "finished_at": datetime.datetime.now(datetime.UTC).isoformat(),
    }


async def _run_orchestrator(
    report_user_id: str, acting_user_id: str, body: dict
) -> dict:
    session = _open_session()
    try:
        pair_scope = resolve_pair_scope(session, report_user_id, acting_user_id)
        if pair_scope is None:
            return {"status": "rejected", "reason": "no active relationship"}

        owner_scope = OwnerScope(owner_user_id=report_user_id, session=session)
        orchestrator = RollingAgendaOrchestrator(
            name="agenda_orchestrator",
            pair_scope=pair_scope,
            owner_scope=owner_scope,
            clock=SystemClock(),
        )

        initial_state = {k: v for k, v in body.items() if k != "report_user_id"}
        session_service = InMemorySessionService()
        adk_session_id = str(uuid.uuid4())
        await session_service.create_session(
            app_name=_AGENDA_TRIGGER_APP_NAME,
            user_id=acting_user_id,
            session_id=adk_session_id,
            state=initial_state,
        )
        runner = Runner(
            agent=orchestrator,
            app_name=_AGENDA_TRIGGER_APP_NAME,
            session_service=session_service,
            plugins=GUARDRAIL_PLUGINS,
        )
        async for _event in runner.run_async(
            user_id=acting_user_id,
            session_id=adk_session_id,
            new_message=types.Content(
                role="user", parts=[types.Part.from_text(text="go")]
            ),
        ):
            pass

        adk_session = await session_service.get_session(
            app_name=_AGENDA_TRIGGER_APP_NAME,
            user_id=acting_user_id,
            session_id=adk_session_id,
        )
        final_state = dict(adk_session.state)
    finally:
        session.close()

    response_body = {"status": "ok"}
    if "agenda_payload" in final_state:
        response_body["agenda_payload"] = final_state["agenda_payload"]
    return response_body


async def _run_orchestrator_in_background(
    report_user_id: str, acting_user_id: str, body: dict
) -> None:
    meeting_id = body.get("meeting_id")
    try:
        result = await _run_orchestrator(report_user_id, acting_user_id, body)
        logger.info(
            "agenda trigger (background meeting_end) finished report_user_id=%s "
            "status=%s",
            report_user_id,
            result.get("status"),
        )
        if meeting_id:
            _record_meeting_end_status(
                meeting_id,
                acting_user_id,
                "rejected" if result.get("status") == "rejected" else "ok",
                result.get("reason"),
            )
    except Exception as exc:
        logger.exception(
            "agenda trigger (background meeting_end) failed report_user_id=%s",
            report_user_id,
        )
        if meeting_id:
            _record_meeting_end_status(meeting_id, acting_user_id, "error", str(exc))


@router.post("/webhooks/agenda-signal")
async def agenda_signal_webhook(request: Request) -> JSONResponse:
    settings = get_settings()
    if not _token_is_valid(settings, request):
        return JSONResponse(status_code=401, content={"status": "invalid token"})

    try:
        signal = await request.json()

        session = _open_session()
        try:
            if not _acting_user_secret_is_valid(
                session, signal["acting_user_id"], request
            ):
                return JSONResponse(
                    status_code=401, content=_ACTING_USER_SECRET_REJECTION
                )
            result = handle_agenda_signal(session, signal)
        finally:
            session.close()
    except (ValueError, KeyError) as exc:
        return JSONResponse(
            status_code=400, content={"status": "rejected", "reason": str(exc)}
        )
    return JSONResponse(status_code=200, content=result)


@router.post("/webhooks/agenda-trigger")
async def agenda_trigger_webhook(
    request: Request, background_tasks: BackgroundTasks
) -> JSONResponse:
    settings = get_settings()
    if not _token_is_valid(settings, request):
        return JSONResponse(status_code=401, content={"status": "invalid token"})

    try:
        body = await request.json()
        report_user_id = body["report_user_id"]
        acting_user_id = body["acting_user_id"]

        session = _open_session()
        try:
            if not _acting_user_secret_is_valid(session, acting_user_id, request):
                return JSONResponse(
                    status_code=401, content=_ACTING_USER_SECRET_REJECTION
                )
        finally:
            session.close()

        trigger_type = body.get("trigger_type")
        if trigger_type == "meeting_end":
            check_session = _open_session()
            try:
                if (
                    resolve_pair_scope(check_session, report_user_id, acting_user_id)
                    is None
                ):
                    return JSONResponse(
                        status_code=200,
                        content={
                            "status": "rejected",
                            "reason": "no active relationship",
                        },
                    )
            finally:
                check_session.close()
            background_tasks.add_task(
                _run_orchestrator_in_background, report_user_id, acting_user_id, body
            )
            return JSONResponse(status_code=202, content={"status": "accepted"})
        response_body = await _run_orchestrator(report_user_id, acting_user_id, body)
    except (ValueError, KeyError) as exc:
        return JSONResponse(
            status_code=400, content={"status": "rejected", "reason": str(exc)}
        )

    return JSONResponse(status_code=200, content=response_body)


@router.get("/webhooks/agenda-trigger-status")
async def agenda_trigger_status_webhook(request: Request) -> JSONResponse:
    settings = get_settings()
    if not _token_is_valid(settings, request):
        return JSONResponse(status_code=401, content={"status": "invalid token"})

    meeting_id = request.query_params.get("meeting_id")
    acting_user_id = request.query_params.get("acting_user_id")
    if not meeting_id or not acting_user_id:
        return JSONResponse(
            status_code=400,
            content={"status": "rejected", "reason": "meeting_id and acting_user_id required"},
        )

    session = _open_session()
    try:
        if not _acting_user_secret_is_valid(session, acting_user_id, request):
            return JSONResponse(status_code=401, content=_ACTING_USER_SECRET_REJECTION)
    finally:
        session.close()

    entry = _meeting_end_status.get(meeting_id)
    if entry is None or entry["acting_user_id"] != acting_user_id:
        return JSONResponse(status_code=200, content={"status": "pending"})

    return JSONResponse(
        status_code=200,
        content={"status": entry["status"], "detail": entry["detail"]},
    )


@router.get("/webhooks/agenda-payload")
async def agenda_payload_webhook(request: Request) -> JSONResponse:
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

            pair_scope = resolve_pair_scope(session, report_user_id, acting_user_id)
            if pair_scope is None:
                return JSONResponse(
                    status_code=200,
                    content={"status": "rejected", "reason": "no active relationship"},
                )

            agenda = get_agenda(pair_scope)
            items = [_item_to_dict(item) for item in agenda if item.status == "open"]
            pending_consent_items = [
                _item_to_dict(item)
                for item in agenda
                if item.status == "pending_consent"
            ]
            payload = build_a2ui_payload(items, pending_consent_items)
        finally:
            session.close()
    except (ValueError, KeyError) as exc:
        return JSONResponse(
            status_code=400, content={"status": "rejected", "reason": str(exc)}
        )

    return JSONResponse(status_code=200, content=payload)
