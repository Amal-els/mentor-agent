"""Unit tests for the pieces of app/tools/mcp_config.py testable without a
real MCP server: FileTokenStorage's own read/write round-trip, and
fathom_mcp_spec's pure construction (real client_metadata requirements —
client_name, redirect_uri, scope — confirmed live this session, see
fathom_mcp_spec's own docstring; the OAuth handshake itself needs a real
server and a real human browser click, covered only by
`mentor fathom-auth` run for real, not by a unit test)."""

import asyncio

from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from app.tools.mcp_config import FileTokenStorage, fathom_mcp_spec


def test_file_token_storage_round_trips_tokens(tmp_path):
    storage = FileTokenStorage(tmp_path / "token.json")
    assert storage.has_tokens() is False
    assert asyncio.run(storage.get_tokens()) is None

    token = OAuthToken(access_token="abc123", token_type="Bearer", expires_in=3600)
    asyncio.run(storage.set_tokens(token))

    assert storage.has_tokens() is True
    loaded = asyncio.run(storage.get_tokens())
    assert loaded.access_token == "abc123"
    assert loaded.token_type == "Bearer"


def test_file_token_storage_round_trips_client_info(tmp_path):
    storage = FileTokenStorage(tmp_path / "token.json")
    assert asyncio.run(storage.get_client_info()) is None

    info = OAuthClientInformationFull(
        client_id="fake-client-id",
        redirect_uris=["http://localhost:8765/callback"],
    )
    asyncio.run(storage.set_client_info(info))

    loaded = asyncio.run(storage.get_client_info())
    assert loaded.client_id == "fake-client-id"


def test_file_token_storage_persists_across_instances(tmp_path):
    """A fresh FileTokenStorage pointed at the same path (the shape
    `mentor fathom-auth` and LiveFathomClient each use — a new instance
    per call, not a shared object) must see what an earlier instance
    wrote — the whole point of persisting to disk instead of memory."""
    path = tmp_path / "token.json"
    token = OAuthToken(access_token="xyz", token_type="Bearer")
    asyncio.run(FileTokenStorage(path).set_tokens(token))

    loaded = asyncio.run(FileTokenStorage(path).get_tokens())
    assert loaded.access_token == "xyz"


def test_file_token_storage_survives_a_missing_file(tmp_path):
    storage = FileTokenStorage(tmp_path / "does_not_exist" / "token.json")
    assert asyncio.run(storage.get_tokens()) is None
    assert storage.has_tokens() is False


def test_fathom_mcp_spec_sets_the_client_name_notion_required(tmp_path):
    """Real, live-confirmed requirement: Fathom's authorization server
    rejects dynamic client registration with "client_name is required"
    without this — the SDK's own default (None) is not enough."""
    spec = fathom_mcp_spec(FileTokenStorage(tmp_path / "token.json"))

    assert spec.url == "https://api.fathom.ai/mcp"
    assert spec.client_metadata.client_name == "Mentor Agent"
    assert str(spec.client_metadata.redirect_uris[0]) == "http://localhost:8765/callback"
    assert "authorization_code" in spec.client_metadata.grant_types
