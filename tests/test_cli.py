from typer.testing import CliRunner

from app.core.llm import creds_available
from app.cli import app

runner = CliRunner()


def test_seed_command_reports_row_counts():
    result = runner.invoke(app, ["seed", "--fixture", "normal_day"])

    assert result.exit_code == 0, result.output
    assert "usr_amal" in result.output
    assert "events: 3" in result.output
    assert "work_items: 2" in result.output
    assert "messages: 1" in result.output


def test_seed_command_rejects_unknown_fixture():
    result = runner.invoke(app, ["seed", "--fixture", "does_not_exist"])

    assert result.exit_code != 0


def test_seed_command_reports_degraded_sources():
    result = runner.invoke(app, ["seed", "--fixture", "degraded_source"])

    assert result.exit_code == 0, result.output
    assert "degraded: calendar (unauthorized)" in result.output
    assert "degraded: linear (error" in result.output


def test_shortlist_command_prints_ranked_items_and_score_terms():
    runner.invoke(app, ["seed", "--fixture", "normal_day"])

    result = runner.invoke(
        app, ["shortlist", "--fixture", "normal_day", "--trigger", "pull"]
    )

    assert result.exit_code == 0, result.output
    assert "score=" in result.output
    assert "candidate_focus=True" in result.output
    assert "owed:" in result.output


def test_shortlist_command_cron_reports_series_suppression_removal():
    runner.invoke(app, ["seed", "--fixture", "series_suppression"])

    result = runner.invoke(
        app, ["shortlist", "--fixture", "series_suppression", "--trigger", "cron"]
    )

    assert result.exit_code == 0, result.output
    assert "removed: " in result.output
    assert "series_suppression:series:standup" in result.output


def test_pulse_command_prints_one_to_three_items_with_why_nows_and_prompt_ids():
    runner.invoke(app, ["seed", "--fixture", "normal_day"])

    result = runner.invoke(app, ["pulse", "--fixture", "normal_day"])

    assert result.exit_code == 0, result.output
    assert "why now:" in result.output
    assert "pulse_ranker.v1" in result.output
    assert "pulse_writer.v1" in result.output
    assert "pulse_critic.v1" in result.output


def test_pulse_command_defaults_to_pull_trigger():
    runner.invoke(app, ["seed", "--fixture", "normal_day"])

    result = runner.invoke(app, ["pulse", "--fixture", "normal_day"])

    assert result.exit_code == 0, result.output
    assert "trigger='pull'" in result.output


def test_pulse_command_defaults_to_normal_day_fixture_matching_the_plans_verification_block():
    runner.invoke(app, ["seed", "--fixture", "normal_day"])

    result = runner.invoke(app, ["pulse", "--user", "usr_amal", "--trigger", "pull"])

    assert result.exit_code == 0, result.output
    assert "fixture='normal_day'" in result.output


def test_pulse_command_prints_degraded_when_no_creds_configured():
    # this sandbox has no LLM credentials configured — assert the CLI says
    # so plainly rather than silently shipping the fallback as if it were
    # the real prompt output
    if creds_available():
        import pytest

        pytest.skip("credentials ARE configured in this environment")

    runner.invoke(app, ["seed", "--fixture", "normal_day"])

    result = runner.invoke(app, ["pulse", "--fixture", "normal_day"])

    assert result.exit_code == 0, result.output
    assert "DEGRADED" in result.output
    assert "creds_present=False" in result.output


def test_pulse_command_dry_run_cron_matches_the_plans_demo_command():
    runner.invoke(app, ["seed", "--fixture", "normal_day"])

    result = runner.invoke(
        app,
        [
            "pulse",
            "--fixture",
            "normal_day",
            "--user",
            "usr_amal",
            "--trigger",
            "cron",
            "--dry-run",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "why now:" in result.output
    assert "window_reason=" in result.output


def test_pulse_command_dry_run_never_persists_a_delivery():
    runner.invoke(app, ["seed", "--fixture", "normal_day"])

    runner.invoke(
        app,
        ["pulse", "--fixture", "normal_day", "--trigger", "cron", "--dry-run"],
    )
    second = runner.invoke(
        app,
        ["pulse", "--fixture", "normal_day", "--trigger", "cron", "--dry-run"],
    )

    assert second.exit_code == 0, second.output
    assert "already delivered" not in second.output


def test_pulse_command_second_cron_without_dry_run_is_idempotent():
    runner.invoke(app, ["seed", "--fixture", "normal_day"])

    runner.invoke(app, ["pulse", "--fixture", "normal_day", "--trigger", "cron"])
    second = runner.invoke(
        app, ["pulse", "--fixture", "normal_day", "--trigger", "cron"]
    )

    assert second.exit_code == 0, second.output
    assert "already delivered" in second.output


def test_pulse_command_second_pull_same_day_is_not_blocked():
    runner.invoke(app, ["seed", "--fixture", "normal_day"])

    runner.invoke(app, ["pulse", "--fixture", "normal_day", "--trigger", "pull"])
    second = runner.invoke(
        app, ["pulse", "--fixture", "normal_day", "--trigger", "pull"]
    )

    assert second.exit_code == 0, second.output
    assert "already delivered" not in second.output
    assert "why now:" in second.output


def test_ingest_command_is_equivalent_to_seed():
    result = runner.invoke(app, ["ingest", "--fixture", "normal_day"])

    assert result.exit_code == 0, result.output
    assert "events: 3" in result.output


def test_demo_card_is_self_seeding_and_prints_both_formats():
    result = runner.invoke(app, ["demo-card", "--fixture", "clear_day"])

    assert result.exit_code == 0, result.output
    assert "--- text ---" in result.output
    assert "--- blocks (Block Kit JSON) ---" in result.output
    assert "Suggested focus" in result.output


def test_demo_card_rejects_unknown_fixture():
    result = runner.invoke(app, ["demo-card", "--fixture", "does_not_exist"])

    assert result.exit_code != 0


def test_validate_command_reports_all_fixtures_valid():
    result = runner.invoke(app, ["validate"])

    assert result.exit_code == 0, result.output
    assert "fixtures valid" in result.output
    assert "ok: normal_day" in result.output


def test_pulse_command_refuses_cross_user_pull():
    runner.invoke(app, ["seed", "--fixture", "normal_day"])

    result = runner.invoke(
        app,
        [
            "pulse",
            "--fixture",
            "normal_day",
            "--user",
            "someone_else",
            "--trigger",
            "pull",
        ],
    )

    assert result.exit_code != 0
    assert "refused" in result.output.lower()


def test_pulse_command_without_deliver_to_never_touches_slack(monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError(
            "should never construct a SlackDeliverer without --deliver-to"
        )

    monkeypatch.setattr("app.cli.SlackDeliverer", _boom)
    runner.invoke(app, ["seed", "--fixture", "normal_day"])

    result = runner.invoke(app, ["pulse", "--fixture", "normal_day"])

    assert result.exit_code == 0, result.output
    assert "delivery:" not in result.output


def test_pulse_command_deliver_to_posts_the_rendered_card(monkeypatch):
    calls = []

    class _FakeDeliverer:
        def __init__(self):
            self.enabled = True

        def deliver(self, user_slack_id, blocks, channel_id=None):
            calls.append((user_slack_id, blocks, channel_id))
            return {"sent": True, "posted_publicly": False, "dm_ts": "123.456"}

    monkeypatch.setattr("app.cli.SlackDeliverer", _FakeDeliverer)
    runner.invoke(app, ["seed", "--fixture", "normal_day"])

    result = runner.invoke(
        app, ["pulse", "--fixture", "normal_day", "--deliver-to", "U0123456"]
    )

    assert result.exit_code == 0, result.output
    assert "delivery: {'sent': True" in result.output
    assert len(calls) == 1
    assert calls[0][0] == "U0123456"
    assert isinstance(calls[0][1], list)


def test_pulse_command_channel_id_passes_through_to_deliverer(monkeypatch):
    calls = []

    class _FakeDeliverer:
        def __init__(self):
            self.enabled = True

        def deliver(self, user_slack_id, blocks, channel_id=None):
            calls.append((user_slack_id, blocks, channel_id))
            return {"sent": True, "posted_publicly": False, "dm_ts": "123.456"}

    monkeypatch.setattr("app.cli.SlackDeliverer", _FakeDeliverer)
    runner.invoke(app, ["seed", "--fixture", "normal_day"])

    result = runner.invoke(
        app,
        [
            "pulse",
            "--fixture",
            "normal_day",
            "--deliver-to",
            "U0123456",
            "--channel-id",
            "C0999999",
        ],
    )

    assert result.exit_code == 0, result.output
    assert len(calls) == 1
    assert calls[0][2] == "C0999999"


def test_pulse_command_deliver_to_skips_when_slack_not_configured(monkeypatch):
    class _DisabledDeliverer:
        def __init__(self):
            self.enabled = False

        def deliver(self, *args, **kwargs):
            raise AssertionError("must not call deliver() when disabled")

    monkeypatch.setattr("app.cli.SlackDeliverer", _DisabledDeliverer)
    runner.invoke(app, ["seed", "--fixture", "normal_day"])

    result = runner.invoke(
        app, ["pulse", "--fixture", "normal_day", "--deliver-to", "U0123456"]
    )

    assert result.exit_code == 0, result.output
    assert "delivery: skipped" in result.output


def test_seed_command_requires_fixture_or_live():
    result = runner.invoke(app, ["seed"])

    assert result.exit_code != 0
    assert "--fixture is required unless --live is set" in result.output


def test_seed_command_live_requires_user():
    result = runner.invoke(app, ["seed", "--live"])

    assert result.exit_code != 0
    assert "--live requires --user" in result.output


def test_seed_command_live_calls_seed_live_and_reports_counts(monkeypatch):
    calls = []

    def _fake_seed_live(session, owner_user_id):
        calls.append(owner_user_id)
        return {
            "owner_user_id": owner_user_id,
            "events": 2,
            "work_items": 1,
            "messages": 0,
            "commitments": 0,
            "degraded_sources": [
                {"source": "linear", "status": "unauthorized", "stale_as_of": None}
            ],
        }

    monkeypatch.setattr("app.cli.seed_live", _fake_seed_live)

    result = runner.invoke(app, ["seed", "--live", "--user", "usr_amal"])

    assert result.exit_code == 0, result.output
    assert calls == ["usr_amal"]
    assert "seeded live owner=usr_amal" in result.output
    assert "events: 2" in result.output
    assert "degraded: linear (unauthorized)" in result.output


def test_seed_command_live_reports_value_error_from_seed_live(monkeypatch):
    def _boom(session, owner_user_id):
        raise ValueError(f"no such user: {owner_user_id!r}")

    monkeypatch.setattr("app.cli.seed_live", _boom)

    result = runner.invoke(app, ["seed", "--live", "--user", "does_not_exist"])

    assert result.exit_code != 0
    assert "no such user" in result.output


def test_link_slack_command_sets_the_users_slack_user_id():
    # a dedicated, idempotent test user — not usr_amal (the real fixture
    # owner used interactively) or a fresh uuid every run, since a raw
    # insert on a fixed id would duplicate-key on a second run against the
    # same shared dev database (no per-test rollback here).
    import datetime

    from app.core.config import get_settings
    from app.core.db import get_engine, get_session_factory
    from app.core.models import User

    engine = get_engine(get_settings().database_url)
    session = get_session_factory(engine)()
    try:
        if session.get(User, "usr_cli_link_test") is None:
            session.add(
                User(
                    id="usr_cli_link_test",
                    created_at=datetime.datetime.now(datetime.UTC),
                )
            )
            session.commit()
    finally:
        session.close()

    result = runner.invoke(
        app,
        ["link-slack", "--user", "usr_cli_link_test", "--slack-user-id", "U0TESTLINK1"],
    )

    assert result.exit_code == 0, result.output
    assert "linked usr_cli_link_test -> U0TESTLINK1" in result.output

    session = get_session_factory(engine)()
    try:
        user = session.get(User, "usr_cli_link_test")
        assert user.slack_user_id == "U0TESTLINK1"
    finally:
        session.close()


def test_link_slack_command_rejects_unknown_user():
    result = runner.invoke(
        app, ["link-slack", "--user", "does_not_exist", "--slack-user-id", "U0X"]
    )

    assert result.exit_code != 0
    assert "no such user" in result.output.lower()


def test_live_status_reports_unauthorized_without_credentials(monkeypatch):
    monkeypatch.delenv("GOOGLE_OAUTH_CREDENTIALS", raising=False)
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.delenv("LINEAR_API_KEY", raising=False)

    result = runner.invoke(app, ["live-status"])

    assert result.exit_code == 0, result.output
    assert "calendar: unauthorized" in result.output.lower()
    assert "slack: unauthorized" in result.output.lower()
    assert "linear: unauthorized" in result.output.lower()


def test_live_status_reports_healthy_when_credentials_present(monkeypatch):
    monkeypatch.setenv("GOOGLE_OAUTH_CREDENTIALS", "/fake/path.json")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-fake")
    monkeypatch.setenv("LINEAR_API_KEY", "lin_api_fake")

    result = runner.invoke(app, ["live-status"])

    assert result.exit_code == 0, result.output
    assert "calendar: healthy" in result.output.lower()
    assert "slack: healthy" in result.output.lower()
    assert "linear: healthy" in result.output.lower()
