"""Generic MCP stdio client (spawns a server process, calls one or more
tools, tears it down) plus the per-connector server specs — which npm
package to spawn and what env vars it needs — used by app/ingest/
live_source.py's SourceClient implementations. SourceClient.fetch() is
synchronous (M2's protocol); the MCP SDK is asyncio-first, so call_tool()/
McpSession are the sync/async bridge.

call_tool() spawns fresh per call — fine for a connector that only makes
one tool call per fetch() (Calendar, Linear). McpSession instead keeps one
process/session open for several calls in a row — real latency found live:
Slack (list_channels + N x get_channel_history), Jira (search + N x
get_issue_links), and Google Docs (listDriveFiles + N x listComments) each
respawn a whole process + OAuth handshake per call under call_tool(),
which made a 6-doc Google Docs fetch alone take ~43s. Use McpSession for
any connector making more than one call per fetch().

STATUS: tool names/argument shapes come from real introspection
(session.list_tools() against each server, no credentials required for
that); Slack, Linear, Calendar, and Jira have each been proven against a
real server — see docs/plans/morning-pulse.md and app/ingest/
live_source.py's module docstring.

"""

import asyncio
import concurrent.futures
import json
import os
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

# mcp's streamable_http/auth modules depend on (and vendor their own
# pinned build of) a package literally named "httpx2" — a real, separate
# installed distribution, not an alias for plain httpx. Confirmed live:
# OAuthClientProvider is an httpx2.Auth subclass (via __mro__), and
# passing it as `auth=` to a plain httpx.AsyncClient failed with
# "TypeError: Invalid 'auth' argument" — httpx's own isinstance check
# rejects an instance of the wrong (if identically-named) Auth class. Use
# httpx2.AsyncClient here specifically; do not swap to plain `httpx`
# without re-verifying live.
import httpx2
from mcp import ClientSession, StdioServerParameters, stdio_client
from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.auth import (
    AuthorizationCodeResult,
    OAuthClientInformationFull,
    OAuthClientMetadata,
    OAuthToken,
)


class McpToolError(Exception):
    """Raised when a tool call completes but the server reports an error
    (CallToolResult.is_error), or the transport/handshake itself fails."""


@dataclass(frozen=True)
class McpServerSpec:
    command: str
    args: list[str]
    env: dict[str, str]


def _parse_tool_result(result, tool_name: str) -> dict | list:
    if getattr(result, "is_error", False):
        raise McpToolError(f"tool {tool_name!r} returned an error: {result.content!r}")

    if getattr(result, "structured_content", None) is not None:
        return result.structured_content

    texts = [
        block.text for block in result.content if getattr(block, "type", None) == "text"
    ]
    if not texts:
        return []
    try:
        return json.loads(texts[0])
    except json.JSONDecodeError:
        # some servers return human-readable text, not JSON — callers that
        # need structured data will fail their own parsing, which is
        # correct: we must not silently invent structure that isn't there.
        return texts[0]


async def _call_tool_async(
    spec: McpServerSpec, tool_name: str, arguments: dict
) -> dict | list:
    params = StdioServerParameters(command=spec.command, args=spec.args, env=spec.env)
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool(tool_name, arguments)
    return _parse_tool_result(result, tool_name)


def call_tool(spec: McpServerSpec, tool_name: str, arguments: dict) -> dict | list:
    """Raises McpToolError or lets transport/asyncio exceptions propagate —
    callers (app/ingest/live_source.py) are responsible for catching and
    reporting via SourceClient.health().

    Also callable from inside an already-running event loop — app/tools/
    custom_tools.py's
    get_morning_pulse wired app/ingest/seed.py's seed_live() (which calls
    this) into a real ADK conversational tool, which runs inside the ADK
    Runner's own loop. asyncio.run() raises "cannot be called from a
    running event loop" there; a live run caught this for real (silently
    degraded every connector rather than crashing, same as the identical
    bug app/core/adk_runner.py's run_agent_sync had). Same fix: detect a
    running loop and, if there is one, run the bridge in a fresh worker
    thread with its own loop instead."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_call_tool_async(spec, tool_name, arguments))

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            asyncio.run, _call_tool_async(spec, tool_name, arguments)
        )
        return future.result()


class McpSession:
    """A synchronous handle onto one long-lived MCP stdio session — open it
    once per fetch() and call .call(tool_name, arguments) as many times as
    needed; the process spawn + OAuth handshake happens once, not once per
    call (unlike call_tool() above).

    Runs its own dedicated event loop in a background thread for the
    lifetime of the `with` block, independent of whatever loop (if any) the
    caller is on — the same "must work whether or not a loop is already
    running" requirement call_tool() documents, but here solved by owning a
    private loop unconditionally rather than detecting the caller's, since
    the session has to stay open across multiple synchronous .call()s and
    can't be torn down and rebuilt as asyncio.run() would do each time.

    The whole session lifetime (open, every .call(), close) runs as ONE
    continuous coroutine/Task on that background loop, driven by a request
    queue — not one asyncio.run_coroutine_threadsafe() per open/call/close.
    anyio (the MCP SDK's transport layer) enforces that a cancel
    scope/TaskGroup's __aexit__ runs in the same asyncio Task that entered
    it; splitting stdio_client()/ClientSession()'s `async with` open in one
    submitted coroutine and their close in a separate one — each becomes
    its own Task under run_coroutine_threadsafe — trips that check with
    "Attempted to exit cancel scope in a different task than it was
    entered in" (found by the first version of this class, live, the very
    first time __exit__ ran)."""

    def __init__(self, spec: McpServerSpec):
        self._spec = spec
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._queue: asyncio.Queue | None = None
        self._driver_future: concurrent.futures.Future | None = None

    async def _driver(self, ready: concurrent.futures.Future) -> None:
        params = StdioServerParameters(
            command=self._spec.command, args=self._spec.args, env=self._spec.env
        )
        try:
            async with stdio_client(params) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    ready.set_result(None)
                    while True:
                        item = await self._queue.get()
                        if item is None:  # __exit__'s close sentinel
                            break
                        kind, payload, result_future = item
                        try:
                            if kind == "call":
                                tool_name, arguments = payload
                                result = await session.call_tool(tool_name, arguments)
                                parsed = _parse_tool_result(result, tool_name)
                            else:  # "list_tools" — introspection only, see list_tools()
                                parsed = await session.list_tools()
                        except Exception as exc:
                            self._loop.call_soon_threadsafe(
                                result_future.set_exception, exc
                            )
                        else:
                            self._loop.call_soon_threadsafe(
                                result_future.set_result, parsed
                            )
        except Exception as exc:
            if not ready.done():
                ready.set_exception(exc)
            raise

    def __enter__(self) -> "McpSession":
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()
        self._queue = asyncio.Queue()

        ready: concurrent.futures.Future = concurrent.futures.Future()
        self._driver_future = asyncio.run_coroutine_threadsafe(
            self._driver(ready), self._loop
        )
        ready.result(timeout=60)
        return self

    def call(
        self, tool_name: str, arguments: dict, timeout: float | None = None
    ) -> dict | list:
        """timeout is None (block forever, original behavior) for every
        existing caller (Slack/Jira/Google Docs each open a session right
        before making their one batch of calls, so an unbounded wait was
        never actually a real risk there). A caller holding a session
        open across its own much longer lifetime — app.triggers.
        agenda_scheduler's meeting-end poller, which reuses one session
        for the whole process — should pass a real timeout: if the
        session's background driver ever dies or its transport breaks,
        an unbounded .result() would otherwise hang this call (and every
        future one on the same broken session) forever, with no
        exception, no log line, silently stalling this process's entire
        single-threaded poll loop until an operator notices and manually
        restarts it. A finite timeout turns that into a raised (and
        therefore loggable, catchable, retryable-next-poll) exception
        instead — still doesn't self-heal the underlying broken session,
        but that's a real, separate reconnect-logic gap, not something a
        timeout alone can fix."""
        result_future: concurrent.futures.Future = concurrent.futures.Future()
        self._loop.call_soon_threadsafe(
            self._queue.put_nowait, ("call", (tool_name, arguments), result_future)
        )
        return result_future.result(timeout=timeout)

    def list_tools(self):
        """Introspection only — the same real-schema-first approach every
        connector in app/ingest/live_source.py was built with (session.
        list_tools() against the real server, no credentials required
        beyond what opening the session already needs). Not used by any
        connector's fetch() path."""
        result_future: concurrent.futures.Future = concurrent.futures.Future()
        self._loop.call_soon_threadsafe(
            self._queue.put_nowait, ("list_tools", None, result_future)
        )
        return result_future.result()

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, None)
            self._driver_future.result(timeout=10)
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=5)
            self._loop.close()


@dataclass(frozen=True)
class HttpMcpServerSpec:
    """The HTTP+OAuth counterpart to McpServerSpec, for remote MCP servers
    that aren't a local npx process — currently only Fathom's official
    server (https://api.fathom.ai/mcp). Confirmed live this session:
    OAuthClientProvider (mcp.client.auth) is an httpx2.Auth subclass (the
    "httpx2" package mcp itself depends on, NOT plain httpx — see this
    module's own import comment), so the transport is
    streamable_http_client(url, http_client=httpx2.
    AsyncClient(auth=oauth_provider)) rather than stdio_client(params) —
    see call_tool_http/HttpMcpSession below, which otherwise mirror
    call_tool/McpSession exactly."""

    url: str
    token_storage: "FileTokenStorage"
    client_metadata: OAuthClientMetadata


class FileTokenStorage(TokenStorage):
    """Persists OAuth tokens + dynamically-registered client info to a
    JSON file. First time this app itself owns OAuth token storage —
    every other OAuth integration here (Calendar, Google Docs) delegates
    that entirely to the spawned npx server process, which has no
    equivalent for a remote server this app talks to directly. Mirrors
    where those spawned servers keep their own token file (e.g.
    ~/.config/google-docs-mcp/token.json) in spirit, just owned by this
    app's own code instead."""

    def __init__(self, path: Path):
        self._path = path

    def _read(self) -> dict:
        if not self._path.exists():
            return {}
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def _write(self, data: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(data), encoding="utf-8")

    def has_tokens(self) -> bool:
        return "tokens" in self._read()

    async def get_tokens(self) -> OAuthToken | None:
        raw = self._read().get("tokens")
        return OAuthToken.model_validate(raw) if raw else None

    async def set_tokens(self, tokens: OAuthToken) -> None:
        data = self._read()
        data["tokens"] = tokens.model_dump(mode="json")
        self._write(data)

    async def get_client_info(self) -> OAuthClientInformationFull | None:
        raw = self._read().get("client_info")
        return OAuthClientInformationFull.model_validate(raw) if raw else None

    async def set_client_info(self, client_info: OAuthClientInformationFull) -> None:
        data = self._read()
        data["client_info"] = client_info.model_dump(mode="json")
        self._write(data)


async def _call_tool_http_async(
    spec: HttpMcpServerSpec,
    tool_name: str,
    arguments: dict,
    redirect_handler: Callable[[str], Awaitable[None]] | None = None,
    callback_handler: Callable[[], Awaitable["AuthorizationCodeResult"]] | None = None,
) -> dict | list:
    oauth = OAuthClientProvider(
        server_url=spec.url,
        client_metadata=spec.client_metadata,
        storage=spec.token_storage,
        redirect_handler=redirect_handler,
        callback_handler=callback_handler,
    )
    async with httpx2.AsyncClient(auth=oauth) as http_client:
        async with streamable_http_client(
            spec.url, http_client=http_client, terminate_on_close=True
        ) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, arguments)
    return _parse_tool_result(result, tool_name)


def call_tool_http(
    spec: HttpMcpServerSpec,
    tool_name: str,
    arguments: dict,
    redirect_handler: Callable[[str], Awaitable[None]] | None = None,
    callback_handler: Callable[[], Awaitable["AuthorizationCodeResult"]] | None = None,
) -> dict | list:
    """Same running-loop bridge as call_tool() — see its own docstring."""
    coro = _call_tool_http_async(
        spec, tool_name, arguments, redirect_handler, callback_handler
    )
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(asyncio.run, coro)
        return future.result()


class HttpMcpSession:
    """The HTTP+OAuth counterpart to McpSession — same .call()/
    .list_tools()/context-manager contract, driven by the identical
    background-loop-plus-queue design (see McpSession's own docstring for
    why: anyio's cancel-scope-must-exit-in-the-same-Task requirement).
    Only the transport differs: streamable_http_client(url, http_client=
    httpx2.AsyncClient(auth=OAuthClientProvider(...))) instead of
    stdio_client(params)."""

    def __init__(
        self,
        spec: HttpMcpServerSpec,
        redirect_handler: Callable[[str], Awaitable[None]] | None = None,
        callback_handler: Callable[[], Awaitable["AuthorizationCodeResult"]]
        | None = None,
    ):
        self._spec = spec
        self._redirect_handler = redirect_handler
        self._callback_handler = callback_handler
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._queue: asyncio.Queue | None = None
        self._driver_future: concurrent.futures.Future | None = None

    async def _driver(self, ready: concurrent.futures.Future) -> None:
        oauth = OAuthClientProvider(
            server_url=self._spec.url,
            client_metadata=self._spec.client_metadata,
            storage=self._spec.token_storage,
            redirect_handler=self._redirect_handler,
            callback_handler=self._callback_handler,
        )
        try:
            async with httpx2.AsyncClient(auth=oauth) as http_client:
                async with streamable_http_client(
                    self._spec.url, http_client=http_client, terminate_on_close=True
                ) as (read, write):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        ready.set_result(None)
                        while True:
                            item = await self._queue.get()
                            if item is None:  # __exit__'s close sentinel
                                break
                            kind, payload, result_future = item
                            try:
                                if kind == "call":
                                    tool_name, arguments = payload
                                    result = await session.call_tool(
                                        tool_name, arguments
                                    )
                                    parsed = _parse_tool_result(result, tool_name)
                                else:  # "list_tools"
                                    parsed = await session.list_tools()
                            except Exception as exc:
                                self._loop.call_soon_threadsafe(
                                    result_future.set_exception, exc
                                )
                            else:
                                self._loop.call_soon_threadsafe(
                                    result_future.set_result, parsed
                                )
        except Exception as exc:
            if not ready.done():
                ready.set_exception(exc)
            raise

    def __enter__(self) -> "HttpMcpSession":
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()
        self._queue = asyncio.Queue()

        ready: concurrent.futures.Future = concurrent.futures.Future()
        self._driver_future = asyncio.run_coroutine_threadsafe(
            self._driver(ready), self._loop
        )
        ready.result(timeout=60)
        return self

    def call(
        self, tool_name: str, arguments: dict, timeout: float | None = None
    ) -> dict | list:
        result_future: concurrent.futures.Future = concurrent.futures.Future()
        self._loop.call_soon_threadsafe(
            self._queue.put_nowait, ("call", (tool_name, arguments), result_future)
        )
        return result_future.result(timeout=timeout)

    def list_tools(self):
        result_future: concurrent.futures.Future = concurrent.futures.Future()
        self._loop.call_soon_threadsafe(
            self._queue.put_nowait, ("list_tools", None, result_future)
        )
        return result_future.result()

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, None)
            self._driver_future.result(timeout=10)
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=5)
            self._loop.close()


def fathom_mcp_spec(token_storage: FileTokenStorage) -> HttpMcpServerSpec:
    """Official Fathom remote MCP server, https://api.fathom.ai/mcp.

    Confirmed live this session, up through generating a real
    authorization URL (the browser-click + callback step itself needs a
    real human and wasn't completable in this automated environment —
    that is `mentor fathom-auth`'s job, still to be run for real):
    - Fathom's authorization server DOES support RFC 7591 dynamic client
      registration — no pre-registered client_id needed. Its only real
      requirement beyond this SDK's own defaults was `client_name`
      (first attempt failed with "400 invalid_client_metadata:
      client_name is required").
    - The redirect_uri below (http://localhost:8765/callback) was
      accepted as-is by registration.
    - `scope` does NOT need to be set here — the resulting authorize URL
      came back with `scope=mcp` filled in by Fathom's own server, not
      something this client had to supply.
    - PKCE (code_challenge/code_challenge_method=S256) is handled
      automatically by OAuthClientProvider — nothing to configure.
    Real authorize URL observed live: `https://fathom.video/mcp/oauth/
    authorize?response_type=code&client_id=...&redirect_uri=http%3A%2F%2F
    localhost%3A8765%2Fcallback&state=...&code_challenge=...&
    code_challenge_method=S256&resource=https%3A%2F%2Fapi.fathom.ai%2Fmcp
    &scope=mcp`."""
    return HttpMcpServerSpec(
        url="https://api.fathom.ai/mcp",
        token_storage=token_storage,
        client_metadata=OAuthClientMetadata(
            redirect_uris=["http://localhost:8765/callback"],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="none",
            # Confirmed live: Fathom's authorization server does support
            # RFC 7591 dynamic client registration, but its first
            # rejection ("client_name is required") is the reason this
            # field exists at all — its own default (None) was not
            # sufficient.
            client_name="Mentor Agent",
        ),
    )


def default_fathom_token_path() -> Path:
    return Path.home() / ".config" / "mentor" / "fathom_token.json"


# --- Per-connector server specs ---------------------------------------------
# One factory per connector: which package to spawn (via npx) and what env
# vars it needs. Credentials are passed in, not read from os.environ here —
# app/ingest/live_source.py's client classes own reading the environment
# (or an explicit constructor override) and pass the resolved values through.


def calendar_mcp_spec(
    oauth_credentials_path: str, token_path: str | None = None
) -> McpServerSpec:
    """@cocal/google-calendar-mcp — requires a Desktop-app OAuth client
    JSON *and* a one-time interactive consent flow (`npx
    @cocal/google-calendar-mcp auth`).

    token_path maps to GOOGLE_CALENDAR_MCP_TOKEN_PATH — confirmed via the
    package's own build/index.js (getSecureTokenPath2) to override where
    it reads/writes the refresh token, independent of
    oauth_credentials_path (the shared app-level client id/secret stays
    the same for everyone; only the per-user token cache differs). None
    (the default) leaves the package at its own default token location —
    the single-shared-account behavior every caller had before per-user
    Google credentials existed (app.ingest.google_credential_store)."""
    env = {"GOOGLE_OAUTH_CREDENTIALS": oauth_credentials_path or ""}
    if token_path:
        env["GOOGLE_CALENDAR_MCP_TOKEN_PATH"] = token_path
    return McpServerSpec(
        command="npx",
        args=["-y", "@cocal/google-calendar-mcp"],
        env=env,
    )


def slack_mcp_spec(bot_token: str, team_id: str) -> McpServerSpec:
    """@modelcontextprotocol/server-slack — marked deprecated upstream ("no
    longer supported") as of this writing, still functional."""
    return McpServerSpec(
        command="npx",
        args=["-y", "@modelcontextprotocol/server-slack"],
        env={
            "SLACK_BOT_TOKEN": bot_token or "",
            "SLACK_TEAM_ID": team_id or "",
        },
    )


def linear_mcp_spec(api_key: str) -> McpServerSpec:
    """mcp-server-linear — the spawned server reads its token from
    LINEAR_ACCESS_TOKEN, per the package's README, not our own
    LINEAR_API_KEY config name — a live run caught this exact mismatch
    (server started with no token and every tool call failed)."""
    return McpServerSpec(
        command="npx",
        args=["-y", "mcp-server-linear"],
        env={"LINEAR_ACCESS_TOKEN": api_key or ""},
    )


def jira_mcp_spec(base_url: str, email: str, api_token: str) -> McpServerSpec:
    """mcp-jira-cloud — see app/ingest/live_source.py's LiveJiraClient
    docstring for what a live round-trip against this server found."""
    return McpServerSpec(
        command="npx",
        args=["-y", "mcp-jira-cloud@latest"],
        env={
            "JIRA_BASE_URL": base_url or "",
            "JIRA_EMAIL": email or "",
            "JIRA_API_TOKEN": api_token or "",
        },
    )


def notion_mcp_spec(notion_token: str) -> McpServerSpec:
    """@notionhq/notion-mcp-server (official) — real tool names confirmed
    live this session (session.list_tools()): API-post-search,
    API-query-data-source, API-retrieve-a-data-source, API-patch-page,
    API-post-page, among 24 total. Single shared NOTION_TOKEN (a Notion
    "internal integration" secret) for the whole workspace — not
    per-user; each database must be explicitly shared with the
    integration from inside Notion (confirmed live: an unshared db yields
    an empty API-post-search result, not an auth error).

    Confirmed live and worth flagging: a data_source_id is NOT stable —
    editing a database's schema can mint a new data_source_id for the
    same database (a live "Career Goals" query 404'd with
    object_not_found shortly after its schema was edited, despite the id
    coming from a real search minutes earlier). LiveNotionGoalsClient
    resolves data source ids by title via API-post-search on every
    fetch() rather than hardcoding them, for exactly this reason."""
    return McpServerSpec(
        command="npx",
        args=["-y", "@notionhq/notion-mcp-server"],
        env={"NOTION_TOKEN": notion_token or ""},
    )


def google_docs_mcp_spec(
    client_id: str, client_secret: str, profile: str | None = None
) -> McpServerSpec:
    """@a-bonus/google-docs-mcp — a different auth shape from Calendar's
    GOOGLE_OAUTH_CREDENTIALS-file convention: this server wants a bare
    GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET pair (the same values already
    inside the Calendar OAuth client JSON — no second OAuth client needed,
    scopes are requested at consent time, not baked into the client) and
    stores its own refresh token separately at
    ~/.config/google-docs-mcp/token.json (or ~/.config/google-docs-mcp/
    <profile>/token.json when profile is set) after a one-time `npx -y
    @a-bonus/google-docs-mcp auth` consent flow. That flow requests a
    fixed set of scopes (Docs/Drive/Sheets/Gmail-modify/Calendar-events)
    regardless of which tool is actually called — Google rejects any of
    them with a plain "invalid_request" until *all* are added to the
    project's OAuth consent screen's configured scope list, not just the
    corresponding API being enabled.

    profile maps to GOOGLE_MCP_PROFILE — confirmed via the package's own
    dist/auth.js (getConfigDir) to pick a per-account subdirectory rather
    than the single shared default, which is what
    app.ingest.google_credential_store's materialize_docs_profile writes
    to for a user who's completed the self-service connect flow. None
    (the default) leaves this at the single-shared-account behavior every
    caller had before per-user Google credentials existed."""
    env = {
        "GOOGLE_CLIENT_ID": client_id or "",
        "GOOGLE_CLIENT_SECRET": client_secret or "",
    }
    if profile:
        env["GOOGLE_MCP_PROFILE"] = profile
    return McpServerSpec(
        command="npx",
        args=["-y", "@a-bonus/google-docs-mcp"],
        env=env,
    )


def google_docs_mcp_client_env(profile: str | None) -> tuple[str, str]:
    """Which OAuth (client_id, client_secret) the @a-bonus/google-docs-mcp
    process should authenticate as, given the token profile it will read.
    Unlike the *_mcp_spec builders above (credentials passed in), this is a
    deliberate env read — it's the one place the Desktop-vs-Web client
    choice lives.

    The shared, no-profile token at ~/.config/google-docs-mcp/token.json is
    minted by `npx -y @a-bonus/google-docs-mcp auth`, whose interactive flow
    binds to a random http://localhost:<port> redirect — only a *Desktop*
    OAuth client accepts that. So when profile is None, prefer the Desktop
    client (GMAIL_MCP_CLIENT_ID/SECRET), falling back to GOOGLE_CLIENT_ID/
    SECRET when those aren't set (single-client setups keep working).

    A per-user profile token, by contrast, is written by this app's own
    browser OAuth router (app.triggers.google_oauth_router), which uses the
    *Web* client (GOOGLE_CLIENT_ID/SECRET) with its registered fixed
    redirect URIs — so a profile-scoped spawn must use that same Web client
    or the refresh grant fails with unauthorized_client."""
    if profile:
        return (
            os.environ.get("GOOGLE_CLIENT_ID", ""),
            os.environ.get("GOOGLE_CLIENT_SECRET", ""),
        )
    return (
        os.environ.get("GMAIL_MCP_CLIENT_ID") or os.environ.get("GOOGLE_CLIENT_ID", ""),
        os.environ.get("GMAIL_MCP_CLIENT_SECRET")
        or os.environ.get("GOOGLE_CLIENT_SECRET", ""),
    )
