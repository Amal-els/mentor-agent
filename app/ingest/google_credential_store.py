"""Bridges Postgres-stored per-user Google OAuth credentials
(app.core.models.GoogleCredential) to the on-disk token files/profiles the
Calendar and Docs/Gmail MCP servers actually read from
(app/tools/mcp_config.py's calendar_mcp_spec/google_docs_mcp_spec) — see
app/triggers/google_oauth_router.py's module docstring for the overall
design and why this indirection exists.

Confirmed by reading both packages' own bundled source (cached locally
under npm's _npx cache — not vendored into this repo, just inspected):

- @cocal/google-calendar-mcp honors GOOGLE_CALENDAR_MCP_TOKEN_PATH, an
  arbitrary per-spawn token file location (build/index.js's
  getSecureTokenPath2). A flat {refresh_token, scope, token_type} file at
  that path is auto-migrated into its internal multi-account wrapper on
  first read (loadMultiAccountTokens: "if (parsed.access_token ||
  parsed.refresh_token) { wrap as {normal: parsed} }").
- @a-bonus/google-docs-mcp (also backs Gmail, per app.ingest.live_source's
  LiveGmailClient reusing google_docs_mcp_spec) honors GOOGLE_MCP_PROFILE
  — reads/writes ~/.config/google-docs-mcp/<profile>/token.json
  (dist/auth.js's getConfigDir). sanitizeStoredTokenCredentials only ever
  reads access_token/refresh_token/scope/token_type off that file, same
  flat shape, no wrapper.

Neither package needs a real access_token written here — refresh_token
alone is enough for googleapis' OAuth2Client to mint a fresh one on first
call (the standard "no access_token cached yet" first-run state), so this
deliberately omits access_token/expiry_date rather than trying to track
Google's real access tokens, which are worthless to persist anyway (~1
hour lifetime, and this app never sees the live one either way — only the
spawned MCP process does)."""

import json
import os
import re
from pathlib import Path

from sqlalchemy.orm import Session

from app.core.clock import Clock
from app.core.crypto import decrypt_token, encrypt_token
from app.core.models import GoogleCredential

_PROFILE_UNSAFE_RE = re.compile(r"[^\w-]")

# Fixed scope list @a-bonus/google-docs-mcp's own auth.js SCOPES constant
# requests at consent time (docs/drive/sheets/gmail.modify/calendar.
# events) — Google rejects a consent request for any subset of these once
# the OAuth client's consent screen has all of them configured, so every
# user's GoogleCredential ends up with this same fixed set regardless of
# which of Calendar/Gmail/Docs they actually end up using.
GOOGLE_OAUTH_SCOPES = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/script.external_request",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/calendar.events",
]


def _tokens_base_dir() -> Path:
    override = os.environ.get("MENTOR_GOOGLE_TOKENS_DIR")
    return Path(override) if override else Path.home() / ".config" / "mentor-google-tokens"


def sanitize_profile(user_id: str) -> str:
    """@a-bonus/google-docs-mcp's own GOOGLE_MCP_PROFILE validation
    (auth.js: /^[\\w-]+$/) rejects anything outside that set — a User.id
    is normally already a plain slug (notion-<uuid>, usr_<name>) but this
    guards against any id that isn't rather than letting the spawned
    process crash on a bad env var."""
    return _PROFILE_UNSAFE_RE.sub("_", user_id)


def get_google_credential(session: Session, user_id: str) -> GoogleCredential | None:
    return session.get(GoogleCredential, user_id)


def is_google_connected(session: Session, user_id: str) -> bool:
    return get_google_credential(session, user_id) is not None


def store_google_credential(
    session: Session,
    user_id: str,
    refresh_token: str,
    google_email: str | None,
    clock: Clock,
) -> GoogleCredential:
    now = clock.now()
    encrypted = encrypt_token(refresh_token)
    existing = session.get(GoogleCredential, user_id)
    if existing is None:
        existing = GoogleCredential(
            user_id=user_id,
            refresh_token_encrypted=encrypted,
            scopes=GOOGLE_OAUTH_SCOPES,
            google_email=google_email,
            connected_at=now,
            updated_at=now,
        )
        session.add(existing)
    else:
        existing.refresh_token_encrypted = encrypted
        existing.scopes = GOOGLE_OAUTH_SCOPES
        existing.google_email = google_email
        existing.updated_at = now
    session.commit()
    return existing


def disconnect_google_credential(session: Session, user_id: str) -> bool:
    existing = session.get(GoogleCredential, user_id)
    if existing is None:
        return False
    session.delete(existing)
    session.commit()
    return True


def _token_payload(refresh_token: str, scopes: list[str]) -> dict:
    """access_token is a deliberate placeholder, not omitted — confirmed
    live (a real "Authentication tokens are no longer valid" failure) and
    by reading @cocal/google-calendar-mcp's own source (build/index.js's
    loadAllAccounts): it strictly requires tokens.access_token to be
    truthy or it silently skips the whole account at startup, with no
    refresh-token-only fallback the way plain googleapis OAuth2Client
    behavior would suggest. expiry_date is set safely in the past so the
    placeholder is never mistaken for a real, still-valid access token —
    every real caller must refresh via refresh_token before its first
    actual API call, exactly as intended."""
    return {
        "access_token": "placeholder-forces-immediate-refresh",
        "refresh_token": refresh_token,
        "scope": " ".join(scopes),
        "token_type": "Bearer",
        "expiry_date": 0,
    }


def materialize_calendar_token_path(session: Session, user_id: str) -> str | None:
    """Writes this user's decrypted refresh token to their own token
    file and returns its path — pass as calendar_mcp_spec's
    token_path_override so the spawned @cocal/google-calendar-mcp process
    authenticates as THIS user instead of whatever's at its own default
    location. None if this user hasn't completed the connect flow yet —
    callers fall back to the legacy single shared GOOGLE_OAUTH_CREDENTIALS
    behavior in that case (app.ingest.live_source's *_for_owner helpers)."""
    credential = get_google_credential(session, user_id)
    if credential is None:
        return None
    refresh_token = decrypt_token(credential.refresh_token_encrypted)
    path = _tokens_base_dir() / sanitize_profile(user_id) / "calendar-token.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_token_payload(refresh_token, credential.scopes)))
    return str(path)


def materialize_docs_profile(session: Session, user_id: str) -> str | None:
    """Writes this user's decrypted refresh token to the profile
    @a-bonus/google-docs-mcp (also backs Gmail) will read when spawned
    with GOOGLE_MCP_PROFILE set to the returned value
    (~/.config/google-docs-mcp/<profile>/token.json — that exact location
    is the package's own, not the mentor-google-tokens dir
    materialize_calendar_token_path uses, since GOOGLE_MCP_PROFILE only
    selects a subdirectory name, not an arbitrary path). None if this
    user hasn't completed the connect flow yet."""
    credential = get_google_credential(session, user_id)
    if credential is None:
        return None
    refresh_token = decrypt_token(credential.refresh_token_encrypted)
    profile = sanitize_profile(user_id)
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    path = base / "google-docs-mcp" / profile / "token.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_token_payload(refresh_token, credential.scopes)))
    return profile
