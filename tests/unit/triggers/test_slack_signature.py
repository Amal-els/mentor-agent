import datetime
import hashlib
import hmac

from app.triggers.slack.slack_signature import verify_slack_signature

SECRET = "fake-signing-secret"


def _sign(timestamp: str, body: str, secret: str = SECRET) -> str:
    basestring = f"v0:{timestamp}:{body}".encode()
    digest = hmac.new(secret.encode(), basestring, hashlib.sha256).hexdigest()
    return f"v0={digest}"


def test_valid_signature_is_accepted():
    now = datetime.datetime(2026, 8, 8, 12, 0, 0, tzinfo=datetime.UTC)
    timestamp = str(int(now.timestamp()))
    body = "command=%2Fmentor&text=pulse&user_id=U123"
    signature = _sign(timestamp, body)

    assert verify_slack_signature(SECRET, timestamp, body, signature, now=now) is True


def test_signature_from_the_wrong_secret_is_rejected():
    now = datetime.datetime(2026, 8, 8, 12, 0, 0, tzinfo=datetime.UTC)
    timestamp = str(int(now.timestamp()))
    body = "command=%2Fmentor&text=pulse&user_id=U123"
    signature = _sign(timestamp, body, secret="a-different-secret")

    assert verify_slack_signature(SECRET, timestamp, body, signature, now=now) is False


def test_signature_for_a_tampered_body_is_rejected():
    now = datetime.datetime(2026, 8, 8, 12, 0, 0, tzinfo=datetime.UTC)
    timestamp = str(int(now.timestamp()))
    signature = _sign(timestamp, "command=%2Fmentor&text=pulse&user_id=U123")

    tampered_body = "command=%2Fmentor&text=pulse&user_id=U999"

    assert (
        verify_slack_signature(SECRET, timestamp, tampered_body, signature, now=now)
        is False
    )


def test_stale_timestamp_is_rejected_even_with_a_correct_signature():
    # Slack's own replay-protection rule: reject anything older than 5
    # minutes, even if the HMAC itself is valid — a captured request replayed
    # later must not be honored.
    now = datetime.datetime(2026, 8, 8, 12, 0, 0, tzinfo=datetime.UTC)
    stale_time = now - datetime.timedelta(minutes=10)
    timestamp = str(int(stale_time.timestamp()))
    body = "command=%2Fmentor&text=pulse&user_id=U123"
    signature = _sign(timestamp, body)

    assert verify_slack_signature(SECRET, timestamp, body, signature, now=now) is False


def test_non_numeric_timestamp_is_rejected_rather_than_raising():
    body = "command=%2Fmentor&text=pulse&user_id=U123"
    signature = _sign("not-a-number", body)

    assert verify_slack_signature(SECRET, "not-a-number", body, signature) is False
