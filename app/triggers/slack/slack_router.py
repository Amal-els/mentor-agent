"""FastAPI router for POST /slack/commands (`/mentor pulse`). Kept separate
from app/fast_api_app.py (the ADK scaffold's own server, which mounts this
router) so tests can exercise the route without paying that module's ~20s
import cost (google.auth.default(), Cloud Logging client, ADK app init).

Slack requires an ack within 3 seconds; a live MCP round-trip across three
connectors routinely takes longer, so the actual pulse work
(run_pulse_command) runs as a FastAPI background task after the ack, not
inline in the request."""

from urllib.parse import parse_qsl

from fastapi import APIRouter, BackgroundTasks, Request, Response

from app.core.config import get_settings
from app.core.db import get_engine, get_session_factory
from app.triggers.slack.slack_command import (
    parse_slash_command,
    resolve_owner_user_id,
    run_prep_command,
    run_pulse_command,
    run_review_command,
)
from app.triggers.slack.slack_signature import verify_slack_signature

router = APIRouter()


def _run_pulse_command_with_own_session(owner_user_id: str, channel_id: str) -> None:
    # background_tasks run after the request's own session/scope would
    # already be gone, so this owns a session for the lifetime of the job.
    engine = get_engine(get_settings().database_url)
    session = get_session_factory(engine)()
    try:
        run_pulse_command(session, owner_user_id, channel_id)
    finally:
        session.close()


def _run_prep_command_with_own_session(owner_user_id: str, channel_id: str) -> None:
    # Same background-task-owns-its-own-session reasoning as
    # _run_pulse_command_with_own_session. channel_id is accepted (for a
    # uniform dispatch signature with the pulse path, and to log which
    # channel the command came from) but not threaded into run_prep_command
    # — a dossier only ever goes to the owner's own DM (see run_prep_
    # command's own docstring).
    engine = get_engine(get_settings().database_url)
    session = get_session_factory(engine)()
    try:
        run_prep_command(session, owner_user_id)
    finally:
        session.close()


def _run_review_command_with_own_session(owner_user_id: str, channel_id: str) -> None:
    # Same shape as _run_prep_command_with_own_session — a Friday
    # reflection is DM-only by construction (run_review_command never
    # threads channel_id into pull_friday_review).
    engine = get_engine(get_settings().database_url)
    session = get_session_factory(engine)()
    try:
        run_review_command(session, owner_user_id)
    finally:
        session.close()


@router.post("/slack/commands")
async def slack_commands(
    request: Request, background_tasks: BackgroundTasks
) -> Response:
    raw_body = (await request.body()).decode("utf-8")
    settings = get_settings()

    if not settings.slack_signing_secret or not verify_slack_signature(
        settings.slack_signing_secret,
        request.headers.get("X-Slack-Request-Timestamp", ""),
        raw_body,
        request.headers.get("X-Slack-Signature", ""),
    ):
        return Response(status_code=401, content="invalid signature")

    payload = parse_slash_command(dict(parse_qsl(raw_body)))

    if payload.text not in ("", "pulse", "prep", "review"):
        return Response(
            content=(
                f"`{payload.text}` isn't supported yet — only `/mentor pulse`, "
                "`/mentor prep`, and `/mentor review`."
            ),
            media_type="text/plain",
        )

    engine = get_engine(settings.database_url)
    session = get_session_factory(engine)()
    try:
        owner_user_id = resolve_owner_user_id(session, payload.slack_user_id)
    finally:
        session.close()

    if owner_user_id is None:
        return Response(
            content=(
                "Your Slack account isn't linked to a Mentor Agent user yet "
                "— ask an admin to run `link-slack`."
            ),
            media_type="text/plain",
        )

    if payload.text == "prep":
        background_tasks.add_task(
            _run_prep_command_with_own_session, owner_user_id, payload.channel_id
        )
        return Response(
            content="On it — your dossier is on its way.", media_type="text/plain"
        )

    if payload.text == "review":
        background_tasks.add_task(
            _run_review_command_with_own_session, owner_user_id, payload.channel_id
        )
        return Response(
            content="On it — your Friday reflection is on its way.",
            media_type="text/plain",
        )

    background_tasks.add_task(
        _run_pulse_command_with_own_session, owner_user_id, payload.channel_id
    )
    return Response(
        content="On it — your pulse is on its way.", media_type="text/plain"
    )
