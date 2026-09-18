"""Verifies Slack's request signature (slash commands, interactivity) per
https://api.slack.com/authentication/verifying-requests-from-slack — the
only thing standing between /slack/commands and anyone on the internet
who can guess the URL. Pure function, no I/O, so it's testable without a
running server or a real Slack app."""

import hashlib
import hmac
import time

MAX_REQUEST_AGE_SECONDS = 60 * 5


def verify_slack_signature(
    signing_secret: str,
    timestamp: str,
    body: str,
    signature: str,
    now: float | None = None,
) -> bool:
    try:
        timestamp_int = int(timestamp)
    except ValueError:
        return False

    current_time = now if now is not None else time.time()
    if hasattr(current_time, "timestamp"):
        current_time = current_time.timestamp()
    if abs(current_time - timestamp_int) > MAX_REQUEST_AGE_SECONDS:
        return False

    basestring = f"v0:{timestamp}:{body}".encode()
    digest = hmac.new(signing_secret.encode(), basestring, hashlib.sha256).hexdigest()
    expected_signature = f"v0={digest}"

    return hmac.compare_digest(expected_signature, signature)
