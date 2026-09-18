"""FastAPI router for the push-based connectors (GitHub, Linear, Jira) —
the counterpart to app/triggers/slack_router.py's pull-triggered
/slack/commands, but for data ingestion rather than a ritual trigger.

Why this exists: seed_live() (app/ingest/seed.py) fetches everything fresh
inside the /mentor pulse request path, which is why a pulse routinely
takes 20-70s (docs/plans/morning-pulse.md's latency investigation).
Webhook-fed sources instead upsert continuously in the background via
normalize_work_item, completely decoupled from any pulse request — by the
time someone asks for their pulse, GitHub's data is already in the DB, no
fetch involved. This app is single-tenant in practice today (every live
connector already reads one set of credentials from process-wide env
vars), so every verified event is attributed to
settings.webhook_owner_user_id rather than needing per-repo owner
resolution.

Kept separate from app/fast_api_app.py for the same reason
slack_router.py is: tests can exercise routes without paying that
module's ~20s import cost."""

import logging
import os

from fastapi import APIRouter, Request, Response

from app.core.clock import SystemClock
from app.core.config import get_settings
from app.core.db import get_engine, get_session_factory
from app.core.scope import OwnerScope
from app.ingest.github_webhook import adapt_issue_event, adapt_pull_request_event
from app.ingest.jira_webhook import adapt_issue_event as adapt_jira_issue_event
from app.ingest.linear_webhook import adapt_issue_event as adapt_linear_issue_event
from app.ingest.normalize import normalize_work_item
from app.triggers.webhooks.webhook_signature import (
    verify_github_signature,
    verify_linear_signature,
    verify_shared_token,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _upsert_work_item(owner_user_id: str, raw: dict) -> None:
    engine = get_engine(get_settings().database_url)
    session = get_session_factory(engine)()
    try:
        scope = OwnerScope(owner_user_id=owner_user_id, session=session)
        normalize_work_item(scope, raw, SystemClock())
    finally:
        session.close()


@router.post("/webhooks/github")
async def github_webhook(request: Request) -> Response:
    settings = get_settings()
    raw_body = (await request.body()).decode("utf-8")

    if not settings.github_webhook_secret or not verify_github_signature(
        settings.github_webhook_secret,
        raw_body,
        request.headers.get("X-Hub-Signature-256"),
    ):
        return Response(status_code=401, content="invalid signature")

    if not settings.webhook_owner_user_id:
        logger.warning("github webhook: WEBHOOK_OWNER_USER_ID not configured, dropping event")
        return Response(status_code=200, content="ok")  # ack anyway — GitHub retries on non-2xx

    event_type = request.headers.get("X-GitHub-Event", "")
    payload = await request.json()
    logger.info(
        "github webhook: received event_type=%r action=%r keys=%s",
        event_type,
        payload.get("action"),
        list(payload.keys()),
    )

    raw = None
    if event_type == "pull_request":
        raw = adapt_pull_request_event(payload)
    elif event_type == "issues":
        raw = adapt_issue_event(payload)
    if raw is None:
        logger.info(
            "github webhook: no work item produced for event_type=%r action=%r",
            event_type,
            payload.get("action"),
        )

    if raw is not None:
        try:
            _upsert_work_item(settings.webhook_owner_user_id, raw)
        except Exception:
            logger.exception(
                "github webhook: failed to upsert work item external_id=%s",
                raw.get("external_id"),
            )

    return Response(status_code=200, content="ok")


@router.post("/webhooks/linear")
async def linear_webhook(request: Request) -> Response:
    settings = get_settings()
    raw_body = (await request.body()).decode("utf-8")

    if not settings.linear_webhook_secret or not verify_linear_signature(
        settings.linear_webhook_secret,
        raw_body,
        request.headers.get("Linear-Signature"),
    ):
        return Response(status_code=401, content="invalid signature")

    if not settings.webhook_owner_user_id:
        logger.warning("linear webhook: WEBHOOK_OWNER_USER_ID not configured, dropping event")
        return Response(status_code=200, content="ok")

    payload = await request.json()
    raw = adapt_linear_issue_event(payload)

    if raw is not None:
        try:
            _upsert_work_item(settings.webhook_owner_user_id, raw)
        except Exception:
            logger.exception(
                "linear webhook: failed to upsert work item external_id=%s",
                raw.get("external_id"),
            )

    return Response(status_code=200, content="ok")


@router.post("/webhooks/jira")
async def jira_webhook(request: Request) -> Response:
    """Jira Cloud webhooks don't sign requests at all (no HMAC header
    exists to verify, per app/triggers/webhook_signature.py's module
    docstring) — auth is a shared token baked into the webhook URL itself
    (?token=...), checked with verify_shared_token."""
    settings = get_settings()

    if not settings.jira_webhook_token or not verify_shared_token(
        settings.jira_webhook_token, request.query_params.get("token")
    ):
        return Response(status_code=401, content="invalid token")

    if not settings.webhook_owner_user_id:
        logger.warning("jira webhook: WEBHOOK_OWNER_USER_ID not configured, dropping event")
        return Response(status_code=200, content="ok")

    payload = await request.json()
    base_url = os.environ.get("JIRA_BASE_URL", "")
    raw = adapt_jira_issue_event(payload, base_url)

    if raw is not None:
        try:
            _upsert_work_item(settings.webhook_owner_user_id, raw)
        except Exception:
            logger.exception(
                "jira webhook: failed to upsert work item external_id=%s",
                raw.get("external_id"),
            )

    return Response(status_code=200, content="ok")
