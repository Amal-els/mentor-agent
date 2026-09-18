import hashlib
import hmac

from app.triggers.webhooks.webhook_signature import (
    verify_github_signature,
    verify_linear_signature,
    verify_shared_token,
)

SECRET = "fake-webhook-secret"


def _github_sign(body: str, secret: str = SECRET) -> str:
    digest = hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _linear_sign(body: str, secret: str = SECRET) -> str:
    return hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()


def test_valid_github_signature_is_accepted():
    body = '{"action": "opened"}'
    signature = _github_sign(body)

    assert verify_github_signature(SECRET, body, signature) is True


def test_github_signature_from_the_wrong_secret_is_rejected():
    body = '{"action": "opened"}'
    signature = _github_sign(body, secret="a-different-secret")

    assert verify_github_signature(SECRET, body, signature) is False


def test_github_signature_for_a_tampered_body_is_rejected():
    signature = _github_sign('{"action": "opened"}')
    tampered_body = '{"action": "closed"}'

    assert verify_github_signature(SECRET, tampered_body, signature) is False


def test_github_signature_missing_the_sha256_prefix_is_rejected():
    digest = hmac.new(SECRET.encode(), b'{"a":1}', hashlib.sha256).hexdigest()

    assert verify_github_signature(SECRET, '{"a":1}', digest) is False


def test_github_signature_none_is_rejected_rather_than_raising():
    assert verify_github_signature(SECRET, '{"a":1}', None) is False


def test_valid_linear_signature_is_accepted():
    body = '{"type": "Issue"}'
    signature = _linear_sign(body)

    assert verify_linear_signature(SECRET, body, signature) is True


def test_linear_signature_from_the_wrong_secret_is_rejected():
    body = '{"type": "Issue"}'
    signature = _linear_sign(body, secret="a-different-secret")

    assert verify_linear_signature(SECRET, body, signature) is False


def test_linear_signature_none_is_rejected_rather_than_raising():
    assert verify_linear_signature(SECRET, '{"a":1}', None) is False


def test_matching_shared_token_is_accepted():
    assert verify_shared_token("expected-token", "expected-token") is True


def test_mismatched_shared_token_is_rejected():
    assert verify_shared_token("expected-token", "wrong-token") is False


def test_missing_shared_token_is_rejected_rather_than_raising():
    assert verify_shared_token("expected-token", None) is False
