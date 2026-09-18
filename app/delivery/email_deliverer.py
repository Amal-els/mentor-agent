"""Real email delivery via the same Google Docs/Gmail MCP server already
used for LiveGoogleDocsClient/LiveGmailClient (app/ingest/live_source.py)
— its real "sendEmail" tool ({to, subject, body, cc?, bcc?}, required
[to, subject, body]) confirmed live this session via session.list_tools()
+ its own input schema. Gated behind GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET
being set, same "off (no-op) unless configured" pattern as
SlackDeliverer (app/delivery/slack_deliverer.py) — this app's only other
real delivery channel."""

import os

from app.tools.mcp_config import (
    call_tool,
    google_docs_mcp_client_env,
    google_docs_mcp_spec,
)


def email_enabled() -> bool:
    client_id, client_secret = google_docs_mcp_client_env(profile=None)
    return bool(client_id and client_secret)


class EmailDeliverer:
    def __init__(
        self, client_id: str | None = None, client_secret: str | None = None
    ):
        # Always sends through the shared, no-profile google-docs-mcp token
        # (~/.config/google-docs-mcp/token.json), so it needs that token's
        # Desktop OAuth client — GMAIL_MCP_CLIENT_ID/SECRET, falling back to
        # GOOGLE_CLIENT_ID/SECRET. See google_docs_mcp_client_env.
        env_id, env_secret = google_docs_mcp_client_env(profile=None)
        self.client_id = client_id or env_id
        self.client_secret = client_secret or env_secret

    @property
    def enabled(self) -> bool:
        return bool(self.client_id and self.client_secret)

    def send(self, to: str, subject: str, body: str) -> dict:
        if not self.enabled:
            return {
                "sent": False,
                "reason": "GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET not configured",
            }

        spec = google_docs_mcp_spec(self.client_id, self.client_secret)
        try:
            call_tool(spec, "sendEmail", {"to": to, "subject": subject, "body": body})
        except Exception as exc:
            return {"sent": False, "reason": str(exc)}
        return {"sent": True}
