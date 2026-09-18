import datetime
import json
from pathlib import Path
import typer
import re
from dotenv import load_dotenv
from app.core.clock import FrozenClock
from app.core.config import get_settings
from app.core.db import get_engine, get_session_factory
from app.core.scope import OwnerScope
from app.delivery.cards import build_card, render_blocks, render_text
from app.delivery.slack_deliverer import SlackDeliverer
from app.ingest.fixture_source import (
    FIXTURES_DIR,
    FixtureCalendarClient,
    FixtureJiraClient,
    FixtureLinearClient,
    FixtureSlackClient,
    load_day_fixture,
)
from app.ingest.live_source import (
    LiveCalendarClient,
    LiveGmailClient,
    LiveGoogleDocsClient,
    LiveJiraClient,
    LiveLinearClient,
    LiveNotionGoalsClient,
    LiveNotionNotesClient,
    LiveSlackClient,
)
from app.ingest.seed import seed_fixture, seed_live
from app.salience.pulse.pulse_context import build_pulse_context
from app.triggers.dispatch import handle_trigger
from app.triggers.pulse_trigger import (
    CrossUserPulseRequestError,
    build_cron_trigger,
    build_pull_trigger,
)

load_dotenv()

app = typer.Typer()

@app.callback()
def main() -> None:
    """Mentor Agent CLI. `seed`, `shortlist`, `pulse`, `demo-card`,
    `ingest`, `validate` — every card-rendering command prints Block Kit
    JSON and a plain-text fallback so the ritual is readable without Slack."""

def _make_clients(fixture_data: dict) -> list:
    return [
        FixtureCalendarClient(fixture_data),
        FixtureSlackClient(fixture_data),
        FixtureLinearClient(fixture_data),
        FixtureJiraClient(fixture_data),
    ]

@app.command()
def seed(
    fixture: str = typer.Option(
        None,
        "--fixture",
        help="Fixture name under app/fixtures/pulse/ (no .yaml). Required unless --live.",
    ),
    live: bool = typer.Option(
        False,
        "--live",
        help="Ingest from the real Calendar/Slack/Linear MCP connectors "
        "(app/ingest/live_source.py) instead of a fixture. Requires --user "
        "(an existing user id — see `live-status` for which sources are "
        "configured).",
    ),
    user: str = typer.Option(
        None,
        "--user",
        help="Owner user id to ingest live data for (--live only). Must already "
        "exist — e.g. from a prior `seed --fixture` run.",
    ),
) -> None:
    """Load a day's data into Postgres via the L2/L3 ingestion path — a
    fixture by default, or the real connectors with --live."""
    if live:
        if not user:
            typer.echo("--live requires --user (an existing user id)", err=True)
            raise typer.Exit(code=1)

        engine = get_engine(get_settings().database_url)
        session = get_session_factory(engine)()
        try:
            try:
                counts = seed_live(session, user)
            except ValueError as exc:
                typer.echo(str(exc), err=True)
                raise typer.Exit(code=1) from exc
        finally:
            session.close()

        typer.echo(f"seeded live owner={counts['owner_user_id']}")
    else:
        if not fixture:
            typer.echo("--fixture is required unless --live is set", err=True)
            raise typer.Exit(code=1)
        if not (Path(FIXTURES_DIR) / f"{fixture}.yaml").exists():
            typer.echo(f"no such fixture: {fixture!r}", err=True)
            raise typer.Exit(code=1)

        engine = get_engine(get_settings().database_url)
        session = get_session_factory(engine)()
        try:
            counts = seed_fixture(session, fixture)
        finally:
            session.close()

        typer.echo(f"seeded fixture={fixture!r} owner={counts['owner_user_id']}")

    for key in (
        "events",
        "work_items",
        "messages",
        "commitments",
        "goals",
        "one_on_one_notes",
    ):
        if key in counts:
            typer.echo(f"  {key}: {counts[key]}")
    for degraded in counts["degraded_sources"]:
        suffix = (
            f", stale as of {degraded['stale_as_of']}"
            if degraded["stale_as_of"] is not None
            else ""
        )
        typer.echo(f"  degraded: {degraded['source']} ({degraded['status']}{suffix})")

@app.command(name="purge-test-data")
def purge_test_data(
    user: str = typer.Option(..., "--user", help="Owner user id to clean."),
    confirm: bool = typer.Option(
        False, "--confirm", help="Actually delete the displayed synthetic rows."
    ),
) -> None:
    from sqlalchemy import delete
    from app.core.models import GraphEdge, GraphNode
    marker = re.compile(r"test|demo|fixture|synthetic|eval|fake|mock", re.I)
    models = (Event, Message, WorkItem)
    engine = get_engine(get_settings().database_url)
    session = get_session_factory(engine)()
    try:
        source_rows = []
        for model in models:
            rows = session.query(model).filter_by(owner_user_id=user).all()
            for row in rows:
                values = (
                    getattr(row, "title", None),
                    getattr(row, "external_id", None),
                    getattr(row, "url", None),
                    getattr(row, "body_ref", None),
                )
                if any(value and marker.search(str(value)) for value in values):
                    source_rows.append(row)

        keys = {
            f"{row.source or 'unknown'}:{row.external_id or row.id}"
            for row in source_rows
        }
        graph_nodes = session.query(GraphNode).filter_by(owner_user_id=user).all()
        graph_node_ids = {node.id for node in graph_nodes if node.canonical_key in keys}
        graph_node_ids.update(
            node.id
            for node in graph_nodes
            if marker.search(node.label or "") and node.node_type != "person"
        )
        typer.echo(
            f"synthetic source rows: {len(source_rows)}, graph nodes: {len(graph_node_ids)}"
        )
        for row in source_rows:
            typer.echo(f"  {row.__class__.__name__}: {row.source}:{row.external_id}")
        if not confirm:
            typer.echo("preview only; rerun with --confirm to delete these rows")
            return

        if graph_node_ids:
            session.execute(
                delete(GraphEdge).where(
                    GraphEdge.owner_user_id == user,
                    (GraphEdge.from_node_id.in_(graph_node_ids))
                    | (GraphEdge.to_node_id.in_(graph_node_ids)),
                )
            )
            session.execute(delete(GraphNode).where(GraphNode.id.in_(graph_node_ids)))
        for row in source_rows:
            session.delete(row)
        session.commit()
        typer.echo("deleted synthetic rows; Notion identity data was preserved")
    finally:
        session.close()

@app.command(name="link-slack")
def link_slack(
    user: str = typer.Option(..., "--user", help="Existing owner user id."),
    slack_user_id: str = typer.Option(
        ..., "--slack-user-id", help="Slack user id (e.g. U0123456) that maps to it."
    ),
) -> None:
    from app.core.models import User
    engine = get_engine(get_settings().database_url)
    session = get_session_factory(engine)()
    try:
        row = session.get(User, user)
        if row is None:
            typer.echo(f"no such user: {user!r}", err=True)
            raise typer.Exit(code=1)
        row.slack_user_id = slack_user_id
        session.commit()
    finally:
        session.close()
    typer.echo(f"linked {user} -> {slack_user_id}")

@app.command(name="link-notion")
def link_notion(
    user: str = typer.Option(..., "--user", help="Existing owner user id."),
    notion_owner_email: str = typer.Option(
        ...,
        "--notion-owner-email",
        help="Email on this user's Notion 'person' identity — the email "
        "that appears in Objectives' Owner (people) property for their "
        "OKRs. Confirmed live: Key Results/Career Goals have no Owner "
        "field of their own, so LiveNotionGoalsClient/LiveNotionNotesClient "
        "resolve ownership transitively through Objectives using this.",
    ),
) -> None:
    import os
    from app.core.models import User
    from app.tools.mcp_config import call_tool, notion_mcp_spec
    display_name = None
    person_id = None
    try:
        users_result = call_tool(
            notion_mcp_spec(os.environ.get("NOTION_TOKEN")), "API-get-users", {}
        )
        users = (
            users_result.get("results", users_result)
            if isinstance(users_result, dict)
            else users_result
        )
        for notion_user in users or []:
            person = notion_user.get("person") or {}
            if person.get("email") == notion_owner_email:
                display_name = notion_user.get("name")
                person_id = notion_user.get("id")
                break
    except Exception:
        typer.echo(
            "warning: could not resolve a Notion display name live "
            "(link will still work, login screen will show the email "
            "instead)",
            err=True,
        )
    engine = get_engine(get_settings().database_url)
    session = get_session_factory(engine)()
    try:
        row = session.get(User, user)
        if row is None:
            typer.echo(f"no such user: {user!r}", err=True)
            raise typer.Exit(code=1)
        row.notion_owner_email = notion_owner_email
        if display_name:
            row.notion_display_name = display_name
        if person_id:
            row.notion_person_id = person_id
        session.commit()
    finally:
        session.close()

    typer.echo(f"linked {user} -> {notion_owner_email} ({display_name or 'no display name resolved'})")

@app.command(name="sync-notion-users")
def sync_notion_users() -> None:
    import datetime as datetime_module
    import os
    from app.core.clock import SystemClock
    from app.ingest.live_source import LiveNotionPairClient
    from app.triggers.agenda.agenda_scheduler import (
        run_notion_member_revocation_once,
        run_notion_pair_sync_once,
        run_notion_user_provisioning_once,
    )
    clock = SystemClock()
    client = LiveNotionPairClient()
    base_url = os.environ.get("AGENDA_PUBLIC_BASE_URL", "")
    default_tz = os.environ.get("AGENDA_DEFAULT_TZ", "UTC")
    default_pulse = datetime_module.time.fromisoformat(
        os.environ.get("AGENDA_DEFAULT_PULSE_TIME_LOCAL", "08:30")
    )
    default_cutoff = datetime_module.time.fromisoformat(
        os.environ.get("AGENDA_DEFAULT_LATE_CUTOFF_LOCAL", "21:00")
    )
    engine = get_engine(get_settings().database_url)
    session = get_session_factory(engine)()
    try:
        provisioned = run_notion_user_provisioning_once(
            session,
            client,
            clock,
            base_url,
            default_tz=default_tz,
            default_pulse_fire_time_local=default_pulse,
            default_late_cutoff_local=default_cutoff,
        )
        edges = run_notion_pair_sync_once(session, client, clock)
        revoked = run_notion_member_revocation_once(session, client, clock)
    finally:
        session.close()
    typer.echo(
        f"provisioned {provisioned} new user(s), synced {edges} pair edge(s), "
        f"revoked {revoked} user(s)"
    )

@app.command(name="link-agenda-client")
def link_agenda_client(
    user: str = typer.Option(..., "--user", help="Existing owner user id."),
    secret: str = typer.Option(
        None,
        "--secret",
        help="Explicit agenda_client_secret to set. If omitted, a new one is "
        "generated with secrets.token_urlsafe(32) and echoed back — this is "
        "the only time it is ever shown in plaintext.",
    ),
) -> None:
    import secrets as secrets_module
    from app.core.models import User
    engine = get_engine(get_settings().database_url)
    session = get_session_factory(engine)()
    try:
        row = session.get(User, user)
        if row is None:
            typer.echo(f"no such user: {user!r}", err=True)
            raise typer.Exit(code=1)
        value = secret or secrets_module.token_urlsafe(32)
        row.agenda_client_secret = value
        session.commit()
    finally:
        session.close()

    typer.echo(f"linked {user} -> {value}")

@app.command()
def shortlist(
    fixture: str = typer.Option(
        ..., "--fixture", help="Fixture name under app/fixtures/pulse/ (no .yaml)"
    ),
    trigger: str = typer.Option("pull", "--trigger", help="cron or pull"),
) -> None:
    if not (Path(FIXTURES_DIR) / f"{fixture}.yaml").exists():
        typer.echo(f"no such fixture: {fixture!r}", err=True)
        raise typer.Exit(code=1)
    engine = get_engine(get_settings().database_url)
    session = get_session_factory(engine)()
    try:
        fixture_data = load_day_fixture(fixture)
        owner_id = fixture_data["owner"]["id"]
        scope = OwnerScope(owner_user_id=owner_id, session=session)
        clock = FrozenClock(at=datetime.datetime.fromisoformat(fixture_data["now"]))
        source_clients = [
            FixtureCalendarClient(fixture_data),
            FixtureSlackClient(fixture_data),
            FixtureLinearClient(fixture_data),
            FixtureJiraClient(fixture_data),
        ]
        ctx = build_pulse_context(
            scope,
            clock,
            source_clients,
            trigger=trigger,
            requested_at=clock.now(),
        )
    finally:
        session.close()
    typer.echo(f"shortlist fixture={fixture!r} trigger={trigger!r}")
    for item in ctx.shortlist:
        typer.echo(
            f"  [{item.item_type}] {item.item_id} score={item.score:.2f} "
            f"candidate_focus={item.candidate_focus} terms={item.score_terms}"
        )
    typer.echo("owed:")
    for owed in ctx.owed:
        typer.echo(
            f"  {owed.item_id} {owed.description!r} promised_to={owed.promised_to} "
            f"overdue={owed.overdue}"
        )
    for removal in ctx.removals:
        typer.echo(f"  removed: {removal.item_id} ({removal.reason})")
    for degraded in ctx.degraded_sources:
        typer.echo(f"  degraded: {degraded}")


@app.command()
def pulse(
    fixture: str = typer.Option(
        "normal_day",
        "--fixture",
        help="Fixture name under app/fixtures/pulse/ (no .yaml). Defaults to "
        "normal_day since there is no live connector to pick data from otherwise.",
    ),
    trigger: str = typer.Option("pull", "--trigger", help="cron or pull"),
    user: str = typer.Option(
        None,
        "--user",
        help="Requesting user id (pull only) — must equal the fixture's owner; "
        "no cross-user delegation exists (decision 4). Defaults to the owner.",
    ),
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help="Compute the pulse without checking idempotency or recording a delivery",
    ),
    deliver_to: str = typer.Option(
        None,
        "--deliver-to",
        help="Slack user id (e.g. U0123456) to actually DM the rendered card to, "
        "via SlackDeliverer. Requires SLACK_BOT_TOKEN. Off by default — this is "
        "the only pulse-card path that sends a real message anywhere.",
    ),
    channel_id: str = typer.Option(
        None,
        "--channel-id",
        help="Slack channel id (e.g. C0123456) simulating a channel invocation "
        "alongside --deliver-to: the card still only ever goes to the DM, plus "
        "an ephemeral ack posted in this channel. Ignored without --deliver-to.",
    ),
) -> None:
    """Run the full L1->L6 pipeline (trigger, ranker, writer, critic) and
    print the rendered pulse card content — the M4/M5 demo command."""
    if not (Path(FIXTURES_DIR) / f"{fixture}.yaml").exists():
        typer.echo(f"no such fixture: {fixture!r}", err=True)
        raise typer.Exit(code=1)
    if trigger not in ("cron", "pull"):
        typer.echo(f"unknown trigger: {trigger!r} (must be cron or pull)", err=True)
        raise typer.Exit(code=1)

    engine = get_engine(get_settings().database_url)
    session = get_session_factory(engine)()
    try:
        fixture_data = load_day_fixture(fixture)
        owner_id = fixture_data["owner"]["id"]
        scope = OwnerScope(owner_user_id=owner_id, session=session)
        clock = FrozenClock(at=datetime.datetime.fromisoformat(fixture_data["now"]))
        source_clients = _make_clients(fixture_data)

        if trigger == "cron":
            event = build_cron_trigger(owner_id, clock.now())
        else:
            try:
                event = build_pull_trigger(user or owner_id, owner_id, clock.now())
            except CrossUserPulseRequestError as exc:
                typer.echo(f"refused: {exc}", err=True)
                raise typer.Exit(code=1) from exc

        trigger_result = handle_trigger(
            scope, clock, source_clients, event, dry_run=dry_run
        )
    finally:
        session.close()

    typer.echo(f"pulse fixture={fixture!r} trigger={trigger!r}")
    if trigger_result.idempotent_skip:
        typer.echo(
            f"  already delivered for {trigger_result.local_date} — idempotent skip "
            "(a retried cron cannot deliver twice)"
        )
        return

    result = trigger_result.rendered
    typer.echo(
        f"  window_date={result.context.window_date} "
        f"window_reason={result.context.window_reason!r}"
    )
    if not result.creds_present:
        typer.echo(
            "DEGRADED: no LLM credentials configured (GOOGLE_API_KEY/GEMINI_API_KEY) "
            "— ranker and writer ran their deterministic fallback, not the real "
            "prompts. Not a failure, but the LLM path is unverified this run."
        )
    elif not (result.ranker_used_llm and result.writer_used_llm):
        typer.echo(
            "DEGRADED: credentials are present but the LLM path still fell back "
            "— see logs for the ranker/writer failure."
        )
    card = build_card(trigger_result)
    typer.echo("--- text ---")
    typer.echo(render_text(card))
    typer.echo("--- blocks (Block Kit JSON) ---")
    typer.echo(json.dumps(render_blocks(card), indent=2))
    typer.echo(
        f"footer: creds_present={result.creds_present} "
        f"ranker_used_llm={result.ranker_used_llm} "
        f"writer_used_llm={result.writer_used_llm} "
        f"ranker_fell_back={result.ranker_fell_back} "
        f"critic_revised={result.critic_revised} critic_skipped={result.critic_skipped}"
    )

    if deliver_to is not None:
        deliverer = SlackDeliverer()
        if not deliverer.enabled:
            typer.echo("delivery: skipped — SLACK_BOT_TOKEN not configured", err=True)
        else:
            delivery = deliverer.deliver(
                deliver_to, blocks=render_blocks(card), channel_id=channel_id
            )
            typer.echo(f"delivery: {delivery}")

@app.command()
def ingest(
    fixture: str = typer.Option(
        None,
        "--fixture",
        help="Fixture name under app/fixtures/pulse/ (no .yaml). Required unless --live.",
    ),
    live: bool = typer.Option(
        False,
        "--live",
        help="Ingest from the real Calendar/Slack/Linear MCP connectors instead "
        "of a fixture. Requires --user.",
    ),
    user: str = typer.Option(
        None, "--user", help="Owner user id to ingest live data for (--live only)."
    ),
) -> None:
    """Same as `seed` — the L2 ingestion entrypoint name."""
    seed(fixture=fixture, live=live, user=user)

@app.command(name="demo-card")
def demo_card(
    fixture: str = typer.Option(
        ..., "--fixture", help="Fixture name under app/fixtures/pulse/ (no .yaml)"
    ),
) -> None:
    if not (Path(FIXTURES_DIR) / f"{fixture}.yaml").exists():
        typer.echo(f"no such fixture: {fixture!r}", err=True)
        raise typer.Exit(code=1)

    engine = get_engine(get_settings().database_url)
    session = get_session_factory(engine)()
    try:
        seed_fixture(session, fixture)
        fixture_data = load_day_fixture(fixture)
        owner_id = fixture_data["owner"]["id"]
        scope = OwnerScope(owner_user_id=owner_id, session=session)
        clock = FrozenClock(at=datetime.datetime.fromisoformat(fixture_data["now"]))
        source_clients = _make_clients(fixture_data)
        event = build_pull_trigger(owner_id, owner_id, clock.now())
        trigger_result = handle_trigger(
            scope, clock, source_clients, event, dry_run=True
        )
    finally:
        session.close()
    card = build_card(trigger_result)
    typer.echo(f"demo-card fixture={fixture!r}")
    typer.echo("--- text ---")
    typer.echo(render_text(card))
    typer.echo("--- blocks (Block Kit JSON) ---")
    typer.echo(json.dumps(render_blocks(card), indent=2))

@app.command()
def validate() -> None:
    from scripts.validate_pulse_fixtures import validate_all
    try:
        validated = validate_all()
    except AssertionError as exc:
        typer.echo(f"INVALID: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    for name in validated:
        typer.echo(f"ok: {name}")
    typer.echo(f"{len(validated)} fixtures valid")

@app.command(name="fathom-auth")
def fathom_auth() -> None:
    import asyncio
    import http.server
    import threading
    import webbrowser
    from urllib.parse import parse_qs, urlparse
    from mcp.shared.auth import AuthorizationCodeResult
    from app.tools.mcp_config import (
        FileTokenStorage,
        HttpMcpSession,
        default_fathom_token_path,
        fathom_mcp_spec,
    )
    captured: dict[str, str | None] = {}
    done = threading.Event()
    class _CallbackHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            query = parse_qs(urlparse(self.path).query)
            captured["code"] = query.get("code", [None])[0]
            captured["state"] = query.get("state", [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"Authorized - you can close this tab.")
            done.set()
        def log_message(self, format: str, *args) -> None:
            pass  # quiet — typer.echo below is the actual UI
    server = http.server.HTTPServer(("localhost", 8765), _CallbackHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    async def redirect_handler(url: str) -> None:
        typer.echo(f"Opening browser for Fathom authorization: {url}")
        webbrowser.open(url)

    async def callback_handler() -> AuthorizationCodeResult:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, done.wait, 120)
        if not done.is_set():
            raise TimeoutError("timed out waiting for Fathom OAuth callback")
        return AuthorizationCodeResult(
            code=captured.get("code") or "", state=captured.get("state")
        )
    token_storage = FileTokenStorage(default_fathom_token_path())
    spec = fathom_mcp_spec(token_storage)
    try:
        with HttpMcpSession(
            spec,
            redirect_handler=redirect_handler,
            callback_handler=callback_handler,
        ) as session:
            tools = session.list_tools()
            typer.echo(
                f"Fathom MCP session established. Tools: "
                f"{[t.name for t in tools.tools]}"
            )
    finally:
        server.shutdown()


@app.command(name="live-status")
def live_status() -> None:
    from app.ingest.base import Healthy
    clients = [
        LiveCalendarClient(),
        LiveSlackClient(),
        LiveLinearClient(),
        LiveJiraClient(),
        LiveGoogleDocsClient(),
        LiveGmailClient(),
        LiveNotionGoalsClient(),
        LiveNotionNotesClient(),
    ]
    for client in clients:
        health = client.health()
        status = "healthy" if isinstance(health, Healthy) else "unauthorized"
        typer.echo(f"{client.source}: {status}")

if __name__ == "__main__":
    app()
