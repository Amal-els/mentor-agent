"""1-on-1 notes, commitment/accomplishment ledgers, and meeting
transcripts — the record-keeping side of the manager↔report relationship.
Split out of app.triggers.agenda.agenda_router, where this was the single
largest chunk (~360 of ~1300 lines) — see that module's history."""

import datetime
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from app.agenda.models import Accomplishment
from app.agenda.scope import resolve_pair_scope
from app.core.clock import SystemClock
from app.core.config import get_settings
from app.core.models import Commitment, Goal, OneOnOneNote
from app.core.scope import OwnerScope
from app.ingest.base import Healthy, Window
from app.ingest.live_source import LiveFathomClient
from app.triggers.agenda.webhook_auth import (
    _ACTING_USER_SECRET_REJECTION,
    _acting_user_secret_is_valid,
    _open_session,
    _token_is_valid,
)

router = APIRouter()


@router.get("/webhooks/one-on-one-notes")
async def one_on_one_notes_webhook(request: Request) -> JSONResponse:
    settings = get_settings()
    if not _token_is_valid(settings, request):
        return JSONResponse(status_code=401, content={"status": "invalid token"})

    try:
        report_user_id = request.query_params["report_user_id"]
        acting_user_id = request.query_params["acting_user_id"]
        limit = int(request.query_params.get("limit", "10"))

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

            owner_scope = OwnerScope(owner_user_id=report_user_id, session=session)
            rows = (
                session.execute(
                    owner_scope.query(OneOnOneNote)
                    .order_by(OneOnOneNote.created_at.desc())
                    .limit(limit)
                )
                .scalars()
                .all()
            )
            notes = [
                {
                    "external_id": row.external_id,
                    "title": row.title,
                    "meeting_id": row.meeting_id,
                    "created_at": row.created_at.isoformat(),
                    "linked_key_result_external_ids": row.linked_key_result_external_ids,
                }
                for row in rows
            ]
        finally:
            session.close()
    except (ValueError, KeyError) as exc:
        return JSONResponse(
            status_code=400, content={"status": "rejected", "reason": str(exc)}
        )

    return JSONResponse(status_code=200, content={"status": "ok", "notes": notes})


@router.get("/webhooks/ledgers")
async def ledgers_webhook(request: Request) -> JSONResponse:
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

            owner_scope = OwnerScope(owner_user_id=report_user_id, session=session)
            commitment_rows = (
                session.execute(
                    owner_scope.query(Commitment).order_by(
                        Commitment.promised_at.desc()
                    )
                )
                .scalars()
                .all()
            )
            accomplishment_rows = (
                session.execute(
                    owner_scope.query(Accomplishment).order_by(
                        Accomplishment.occurred_at.desc()
                    )
                )
                .scalars()
                .all()
            )
            commitments = [
                {
                    "id": row.id,
                    "description": row.description,
                    "status": row.status,
                    "promised_to_person_id": row.promised_to_person_id,
                    "promised_at": row.promised_at.isoformat(),
                    "due_at": row.due_at.isoformat() if row.due_at else None,
                    "delivered_at": (
                        row.delivered_at.isoformat() if row.delivered_at else None
                    ),
                }
                for row in commitment_rows
            ]
            goal_ids = {row.goal_id for row in accomplishment_rows if row.goal_id}
            goal_titles = {
                goal.id: goal.title
                for goal in (
                    session.execute(
                        owner_scope.query(Goal).where(Goal.id.in_(goal_ids))
                    )
                    .scalars()
                    .all()
                    if goal_ids
                    else []
                )
            }
            accomplishments = [
                {
                    "id": row.id,
                    "description": row.description,
                    "occurred_at": row.occurred_at.isoformat(),
                    "goal_id": row.goal_id,
                    "goal_title": goal_titles.get(row.goal_id),
                }
                for row in accomplishment_rows
            ]
        finally:
            session.close()
    except (ValueError, KeyError) as exc:
        return JSONResponse(
            status_code=400, content={"status": "rejected", "reason": str(exc)}
        )

    return JSONResponse(
        status_code=200,
        content={
            "status": "ok",
            "commitments": commitments,
            "accomplishments": accomplishments,
        },
    )


@router.post("/webhooks/ledgers/commitment")
async def add_commitment_webhook(request: Request) -> JSONResponse:
    settings = get_settings()
    if not _token_is_valid(settings, request):
        return JSONResponse(status_code=401, content={"status": "invalid token"})

    try:
        body = await request.json()
        owner_user_id = body["owner_user_id"]
        description = body["description"].strip()
        if not description:
            raise ValueError("description must not be empty")
    except (KeyError, ValueError) as exc:
        return JSONResponse(
            status_code=400, content={"status": "rejected", "reason": str(exc)}
        )

    session = _open_session()
    try:
        if not _acting_user_secret_is_valid(session, owner_user_id, request):
            return JSONResponse(
                status_code=401, content=_ACTING_USER_SECRET_REJECTION
            )

        due_at = None
        if body.get("due_at"):
            try:
                due_at = datetime.datetime.fromisoformat(body["due_at"])
            except ValueError:
                return JSONResponse(
                    status_code=400,
                    content={
                        "status": "rejected",
                        "reason": f"due_at must be ISO 8601, got {body['due_at']!r}",
                    },
                )

        owner_scope = OwnerScope(owner_user_id=owner_user_id, session=session)
        now = SystemClock().now()
        row = Commitment(
            id=str(uuid.uuid4()),
            description=description,
            source_reference_key=f"manual:{owner_user_id}",
            promised_at=now,
            due_at=due_at,
            delivered_at=None,
            status="open",
        )
        owner_scope.add(row)
        owner_scope.commit()
        result = {
            "id": row.id,
            "description": row.description,
            "status": row.status,
            "promised_at": row.promised_at.isoformat(),
            "due_at": row.due_at.isoformat() if row.due_at else None,
        }
    finally:
        session.close()

    return JSONResponse(status_code=200, content={"status": "ok", "commitment": result})


@router.post("/webhooks/ledgers/accomplishment")
async def add_accomplishment_webhook(request: Request) -> JSONResponse:
    settings = get_settings()
    if not _token_is_valid(settings, request):
        return JSONResponse(status_code=401, content={"status": "invalid token"})

    try:
        body = await request.json()
        owner_user_id = body["owner_user_id"]
        description = body["description"].strip()
        if not description:
            raise ValueError("description must not be empty")
    except (KeyError, ValueError) as exc:
        return JSONResponse(
            status_code=400, content={"status": "rejected", "reason": str(exc)}
        )

    session = _open_session()
    try:
        if not _acting_user_secret_is_valid(session, owner_user_id, request):
            return JSONResponse(
                status_code=401, content=_ACTING_USER_SECRET_REJECTION
            )

        owner_scope = OwnerScope(owner_user_id=owner_user_id, session=session)
        now = SystemClock().now()

        occurred_at = now
        if body.get("occurred_at"):
            try:
                occurred_at = datetime.datetime.fromisoformat(body["occurred_at"])
            except ValueError:
                return JSONResponse(
                    status_code=400,
                    content={
                        "status": "rejected",
                        "reason": (
                            f"occurred_at must be ISO 8601, got "
                            f"{body['occurred_at']!r}"
                        ),
                    },
                )

        goal_id = body.get("goal_id") or None
        if goal_id is not None:
            goal = session.execute(
                owner_scope.query(Goal).where(Goal.id == goal_id)
            ).scalar_one_or_none()
            if goal is None:
                return JSONResponse(
                    status_code=400,
                    content={
                        "status": "rejected",
                        "reason": f"no such goal for this owner: {goal_id!r}",
                    },
                )

        row = Accomplishment(
            id=str(uuid.uuid4()),
            description=description,
            source_reference_key=f"manual:{owner_user_id}",
            occurred_at=occurred_at,
            goal_id=goal_id,
        )
        owner_scope.add(row)
        owner_scope.commit()
        result = {
            "id": row.id,
            "description": row.description,
            "occurred_at": row.occurred_at.isoformat(),
            "goal_id": row.goal_id,
        }
    finally:
        session.close()

    return JSONResponse(
        status_code=200, content={"status": "ok", "accomplishment": result}
    )


@router.get("/webhooks/fathom-transcript")
async def fathom_transcript_webhook(request: Request) -> JSONResponse:
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
        finally:
            session.close()

        now = datetime.datetime.now(datetime.UTC)
        starts_at_param = request.query_params.get("starts_at")
        ends_at_param = request.query_params.get("ends_at")
        window = Window(
            start=(
                datetime.datetime.fromisoformat(starts_at_param)
                if starts_at_param
                else now - datetime.timedelta(hours=3)
            ),
            end=(
                datetime.datetime.fromisoformat(ends_at_param) if ends_at_param else now
            ),
        )

        client = LiveFathomClient()
        if not isinstance(client.health(), Healthy):
            return JSONResponse(
                status_code=200,
                content={"status": "not_connected", "transcript_text": None},
            )

        transcript_text = client.find_transcript(window)
    except (ValueError, KeyError) as exc:
        return JSONResponse(
            status_code=400, content={"status": "rejected", "reason": str(exc)}
        )

    return JSONResponse(
        status_code=200,
        content={
            "status": "ok" if transcript_text else "no_transcript_found",
            "transcript_text": transcript_text,
        },
    )
