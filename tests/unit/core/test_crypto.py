import pytest
from cryptography.fernet import Fernet

from app.core import crypto


@pytest.fixture(autouse=True)
def _fresh_key(monkeypatch):
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    crypto._fernet.cache_clear()
    yield
    crypto._fernet.cache_clear()


def test_round_trips():
    ciphertext = crypto.encrypt_token("a-real-refresh-token")
    assert ciphertext != "a-real-refresh-token"
    assert crypto.decrypt_token(ciphertext) == "a-real-refresh-token"


def test_missing_key_raises(monkeypatch):
    monkeypatch.delenv("TOKEN_ENCRYPTION_KEY", raising=False)
    crypto._fernet.cache_clear()
    with pytest.raises(crypto.TokenEncryptionKeyMissing):
        crypto.encrypt_token("x")


def test_malformed_key_raises(monkeypatch):
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", "not-a-real-key")
    crypto._fernet.cache_clear()
    with pytest.raises(crypto.TokenEncryptionKeyMissing):
        crypto.encrypt_token("x")


def test_decrypting_with_a_different_key_fails(monkeypatch):
    ciphertext = crypto.encrypt_token("secret")
    monkeypatch.setenv("TOKEN_ENCRYPTION_KEY", Fernet.generate_key().decode())
    crypto._fernet.cache_clear()
    with pytest.raises(ValueError):
        crypto.decrypt_token(ciphertext)
