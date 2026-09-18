import pytest


@pytest.fixture(autouse=True)
def _no_real_llm_calls_by_default(monkeypatch):
    """Unit tests must never silently start hitting a real LLM just because
    credentials happen to be configured in the environment — only
    tests/live/ (gated behind @pytest.mark.live, opts back out of this
    fixture in its own conftest.py) does that deliberately.

    app.pipeline.pulse imports creds_available via `from ... import`, which
    binds its own local name — patching app.core.llm.creds_available
    would not affect that reference, so both are patched here."""
    monkeypatch.setattr("app.core.llm.creds_available", lambda: False)
    monkeypatch.setattr("app.pipeline.pulse.creds_available", lambda: False)


@pytest.fixture(autouse=True)
def _no_real_connector_creds_by_default(monkeypatch):
    """Same problem as above, one layer down: whichever test file happens
    to import app.cli (which calls load_dotenv() at import time) first in
    a pytest session pollutes os.environ for every test that runs after
    it, for the rest of that process — env vars aren't reset between
    tests. A live run caught this for real: test_deliverer_is_a_noop_
    without_a_token started failing only when run as part of the full
    suite (never in isolation), because SLACK_BOT_TOKEN leaked in from an
    earlier test's import and SlackDeliverer(token=None) falls back to
    os.environ.get("SLACK_BOT_TOKEN"). Strip every live-connector
    credential var here so unit tests see "not configured" regardless of
    what's actually set in the environment; tests/live/ overrides this
    back to a no-op."""
    for var in (
        "SLACK_BOT_TOKEN",
        "SLACK_TEAM_ID",
        "LINEAR_API_KEY",
        "GOOGLE_OAUTH_CREDENTIALS",
        "JIRA_BASE_URL",
        "JIRA_EMAIL",
        "JIRA_API_TOKEN",
        "GOOGLE_CLIENT_ID",
        "GOOGLE_CLIENT_SECRET",
    ):
        monkeypatch.delenv(var, raising=False)
