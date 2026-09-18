"""Verifies inbound webhook authenticity for the connectors that push
events instead of being polled (app/triggers/webhook_router.py) — the
webhook equivalent of app/triggers/slack_signature.py. Pure functions, no
I/O, so they're testable without a running server or a real webhook
delivery.

Each source signs differently:
- GitHub: HMAC-SHA256 over the raw body, sent as
  `X-Hub-Signature-256: sha256=<hex>`.
- Linear: HMAC-SHA256 over the raw body, sent as `Linear-Signature: <hex>`
  — no prefix, unlike GitHub's.
- Jira Cloud webhooks don't sign requests at all (no HMAC header exists to
  verify) — the standard workaround is a shared secret token embedded in
  the webhook URL itself (e.g. `?token=...`), checked with
  verify_shared_token."""

import hashlib
import hmac

_GITHUB_SIGNATURE_PREFIX = "sha256="


def verify_github_signature(secret: str, body: str, signature: str | None) -> bool:
    if signature is None or not signature.startswith(_GITHUB_SIGNATURE_PREFIX):
        return False
    provided_digest = signature[len(_GITHUB_SIGNATURE_PREFIX) :]
    expected_digest = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected_digest, provided_digest)


def verify_linear_signature(secret: str, body: str, signature: str | None) -> bool:
    if signature is None:
        return False
    expected_digest = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected_digest, signature)


def verify_shared_token(expected_token: str, provided_token: str | None) -> bool:
    if provided_token is None:
        return False
    return hmac.compare_digest(expected_token, provided_token)
