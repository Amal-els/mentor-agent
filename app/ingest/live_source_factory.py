"""Builds LiveCalendarClient/LiveGoogleDocsClient/LiveGmailClient wired to
a specific owner's own Google credential when they've completed the
self-service connect flow (app.ingest.google_credential_store), falling
back to the legacy single-shared-account behavior (GOOGLE_OAUTH_
CREDENTIALS / GOOGLE_CLIENT_ID+SECRET read from process env, unchanged)
when they haven't — materialize_calendar_token_path/materialize_docs_
profile both return None for an owner with no stored GoogleCredential, and
LiveCalendarClient(token_path=None)/LiveGmailClient(profile=None) are
exactly the pre-multi-tenant constructor calls every existing caller
already made, so this is a strict superset, never a behavior change for
an owner who hasn't connected.

Kept separate from app.ingest.live_source itself, which is deliberately
DB-agnostic (no sqlalchemy Session import anywhere in that module) — this
is the one seam where "which owner is this for" (a DB concern) meets
"build a connector client" (a pure-construction concern)."""

from sqlalchemy.orm import Session

from app.ingest.google_credential_store import (
    materialize_calendar_token_path,
    materialize_docs_profile,
)
from app.ingest.live_source import LiveCalendarClient, LiveGmailClient, LiveGoogleDocsClient


def calendar_client_for(session: Session, owner_user_id: str) -> LiveCalendarClient:
    return LiveCalendarClient(
        token_path=materialize_calendar_token_path(session, owner_user_id)
    )


def gmail_client_for(session: Session, owner_user_id: str) -> LiveGmailClient:
    return LiveGmailClient(profile=materialize_docs_profile(session, owner_user_id))


def google_docs_client_for(session: Session, owner_user_id: str) -> LiveGoogleDocsClient:
    return LiveGoogleDocsClient(profile=materialize_docs_profile(session, owner_user_id))
