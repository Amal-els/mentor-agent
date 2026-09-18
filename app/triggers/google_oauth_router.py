import hmac
import logging
import os
import time
import requests
from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, RedirectResponse
from app.core.clock import SystemClock
from app.core.config import get_settings
from app.core.db import get_engine, get_session_factory
from app.core.models import User
from app.ingest.google_credential_store import (
    GOOGLE_OAUTH_SCOPES,
    disconnect_google_credential,
    get_google_credential,
    store_google_credential,
)
from app.triggers.agenda.webhook_auth import (
    _acting_user_secret_is_valid as _acting_user_secret_is_valid_header,
)
from app.triggers.agenda.webhook_auth import _token_is_valid as _token_is_valid_header
from app.triggers.webhooks.webhook_signature import verify_shared_token
logger = logging.getLogger(__name__)
router = APIRouter()
_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_URL = "https://oauth2.googleapis.com/token"
_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"
_CALLBACK_PATH = "/webhooks/google-oauth/callback"
_STATE_TTL_SECONDS = 600
_IDENTITY_SCOPES = [
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
]


def _open_session():
    return get_session_factory(get_engine(get_settings().database_url))()

def _token_is_valid_qs(settings, request: Request) -> bool:
    return bool(settings.agenda_webhook_token) and verify_shared_token(
        settings.agenda_webhook_token, request.query_params.get("token")
    )

def _acting_user_secret_is_valid_qs(session, owner_user_id: str, request: Request) -> bool:
    provided = request.query_params.get("secret")
    if provided is None:
        return False
    user = session.get(User, owner_user_id)
    if user is None or user.agenda_client_secret is None:
        return False
    return hmac.compare_digest(user.agenda_client_secret, provided)

def _sign_state(owner_user_id: str, secret: str) -> str:
    expires_at = int(time.time()) + _STATE_TTL_SECONDS
    payload = f"{owner_user_id}:{expires_at}"
    digest = hmac.new(secret.encode(), payload.encode(), "sha256").hexdigest()
    return f"{payload}:{digest}"

def _verify_state(state: str, secret: str) -> str | None:
    parts = state.split(":")
    if len(parts) != 3:
        return None
    owner_user_id, expires_at_raw, provided_digest = parts
    payload = f"{owner_user_id}:{expires_at_raw}"
    expected_digest = hmac.new(secret.encode(), payload.encode(), "sha256").hexdigest()
    if not hmac.compare_digest(expected_digest, provided_digest):
        return None
    try:
        expires_at = int(expires_at_raw)
    except ValueError:
        return None
    if time.time() > expires_at:
        return None
    return owner_user_id

@router.get("/webhooks/google-oauth/start")
async def google_oauth_start(request: Request) -> RedirectResponse:
    settings = get_settings()
    owner_user_id = request.query_params.get("owner_user_id")
    if owner_user_id is None or not _token_is_valid_qs(settings, request):
        return RedirectResponse(url="/ui?google_error=unauthorized", status_code=302)

    session = _open_session()
    try:
        if not _acting_user_secret_is_valid_qs(session, owner_user_id, request):
            return RedirectResponse(url="/ui?google_error=unauthorized", status_code=302)
    finally:
        session.close()

    client_id = os.environ.get("GOOGLE_CLIENT_ID")
    if not client_id:
        logger.warning("google oauth: GOOGLE_CLIENT_ID not configured, refusing to start")
        return RedirectResponse(url="/ui?google_error=not_configured", status_code=302)

    redirect_uri = str(request.base_url).rstrip("/") + _CALLBACK_PATH
    state = _sign_state(owner_user_id, settings.agenda_webhook_token)
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(GOOGLE_OAUTH_SCOPES + _IDENTITY_SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    query = "&".join(f"{k}={requests.utils.quote(v, safe='')}" for k, v in params.items())
    return RedirectResponse(url=f"{_AUTHORIZE_URL}?{query}", status_code=302)

@router.get("/webhooks/google-oauth/callback")
async def google_oauth_callback(request: Request) -> RedirectResponse:
    settings = get_settings()
    error = request.query_params.get("error")
    if error:
        logger.info("google oauth: consent denied or errored: %s", error)
        return RedirectResponse(url="/ui?google_error=denied", status_code=302)
    code = request.query_params.get("code")
    state = request.query_params.get("state")
    if not code or not state:
        return RedirectResponse(url="/ui?google_error=bad_request", status_code=302)
    owner_user_id = _verify_state(state, settings.agenda_webhook_token)
    if owner_user_id is None:
        logger.warning("google oauth: invalid or expired state on callback")
        return RedirectResponse(url="/ui?google_error=expired", status_code=302)
    client_id = os.environ.get("GOOGLE_CLIENT_ID")
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET")
    redirect_uri = str(request.base_url).rstrip("/") + _CALLBACK_PATH
    try:
        token_response = requests.post(
            _TOKEN_URL,
            data={
                "code": code,
                "client_id": client_id,
                "client_secret": client_secret,
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
            timeout=15,
        )
        token_response.raise_for_status()
        token_body = token_response.json()
    except requests.RequestException:
        logger.exception("google oauth: token exchange failed for owner=%s", owner_user_id)
        return RedirectResponse(url="/ui?google_error=exchange_failed", status_code=302)
    refresh_token = token_body.get("refresh_token")
    if not refresh_token:
        logger.warning(
            "google oauth: no refresh_token in response for owner=%s (keys=%s)",
            owner_user_id,
            list(token_body.keys()),
        )
        return RedirectResponse(url="/ui?google_error=no_refresh_token", status_code=302)
    google_email = None
    access_token = token_body.get("access_token")
    if access_token:
        try:
            userinfo_response = requests.get(
                _USERINFO_URL,
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=10,
            )
            if userinfo_response.ok:
                google_email = userinfo_response.json().get("email")
        except requests.RequestException:
            logger.warning(
                "google oauth: userinfo lookup failed for owner=%s, "
                "connecting without a displayed email",
                owner_user_id,
            )
    session = _open_session()
    try:
        store_google_credential(
            session, owner_user_id, refresh_token, google_email, SystemClock()
        )
    finally:
        session.close()
    return RedirectResponse(url="/ui?google_connected=1", status_code=302)

@router.get("/webhooks/google-oauth/status")
async def google_oauth_status(request: Request) -> JSONResponse:
    settings = get_settings()
    if not _token_is_valid_header(settings, request):
        return JSONResponse(status_code=401, content={"status": "invalid token"})
    owner_user_id = request.query_params.get("owner_user_id")
    if owner_user_id is None:
        return JSONResponse(status_code=400, content={"status": "owner_user_id required"})
    session = _open_session()
    try:
        if not _acting_user_secret_is_valid_header(session, owner_user_id, request):
            return JSONResponse(
                status_code=401, content={"status": "invalid acting user secret"}
            )
        credential = get_google_credential(session, owner_user_id)
        if credential is None:
            return JSONResponse(content={"connected": False})
        return JSONResponse(
            content={
                "connected": True,
                "google_email": credential.google_email,
                "connected_at": credential.connected_at.isoformat(),
            }
        )
    finally:
        session.close()

@router.post("/webhooks/google-oauth/disconnect")
async def google_oauth_disconnect(request: Request) -> JSONResponse:
    settings = get_settings()
    if not _token_is_valid_header(settings, request):
        return JSONResponse(status_code=401, content={"status": "invalid token"})
    body = await request.json()
    owner_user_id = body.get("owner_user_id")
    if not owner_user_id:
        return JSONResponse(status_code=400, content={"status": "owner_user_id required"})
    session = _open_session()
    try:
        if not _acting_user_secret_is_valid_header(session, owner_user_id, request):
            return JSONResponse(
                status_code=401, content={"status": "invalid acting user secret"}
            )
        disconnect_google_credential(session, owner_user_id)
        return JSONResponse(content={"status": "ok"})
    finally:
        session.close()
