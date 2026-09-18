import datetime

from app.core.models import User
from app.ingest.base import Unauthorized
from app.tools.custom_tools import get_morning_pulse, render_pulse_text_for_owner


class _FakeToolContext:
    def __init__(self, user_id):
        self.user_id = user_id


class _FakeLiveClient:
    def __init__(self, source="calendar"):
        self.source = source

    def health(self):
        return Unauthorized()

    def fetch(self, window, owner_user_id):
        return []


def test_render_pulse_text_for_owner_rejects_unknown_user(pg_session, monkeypatch):
    monkeypatch.setattr("app.tools.custom_tools.get_engine", lambda url: pg_session.bind)
    monkeypatch.setattr(
        "app.tools.custom_tools.get_session_factory", lambda engine: lambda: pg_session
    )

    text = render_pulse_text_for_owner("does_not_exist")

    assert "No Mentor Agent user found" in text


def test_render_pulse_text_for_owner_renders_a_clear_day_for_a_seeded_user(
    pg_session, monkeypatch
):
    # get-or-create: this test shares a database across runs (no per-test
    # rollback), so a fixed id must be idempotent rather than a raw insert.
    if pg_session.get(User, "usr_tool1") is None:
        pg_session.add(
            User(id="usr_tool1", created_at=datetime.datetime.now(datetime.UTC))
        )
        pg_session.commit()

    monkeypatch.setattr("app.tools.custom_tools.get_engine", lambda url: pg_session.bind)
    monkeypatch.setattr(
        "app.tools.custom_tools.get_session_factory", lambda engine: lambda: pg_session
    )
    monkeypatch.setattr(
        "app.tools.custom_tools.seed_live", lambda session, owner_id, clock=None: None
    )
    monkeypatch.setattr(
        "app.tools.custom_tools.calendar_client_for",
        lambda session, owner_user_id: _FakeLiveClient("calendar"),
    )
    monkeypatch.setattr(
        "app.tools.custom_tools.LiveSlackClient", lambda **kwargs: _FakeLiveClient("slack")
    )
    monkeypatch.setattr(
        "app.tools.custom_tools.LiveLinearClient", lambda: _FakeLiveClient("linear")
    )
    monkeypatch.setattr(
        "app.tools.custom_tools.LiveJiraClient", lambda: _FakeLiveClient("jira")
    )

    text = render_pulse_text_for_owner("usr_tool1")

    assert isinstance(text, str)
    assert text.strip()


def test_get_morning_pulse_uses_the_tool_contexts_user_id_not_a_model_argument(
    monkeypatch,
):
    captured = {}

    def fake_render(owner_user_id):
        captured["owner_user_id"] = owner_user_id
        return "rendered pulse text"

    monkeypatch.setattr("app.tools.custom_tools.render_pulse_text_for_owner", fake_render)

    result = get_morning_pulse(_FakeToolContext(user_id="usr_real_session_user"))

    assert captured["owner_user_id"] == "usr_real_session_user"
    assert result == {"status": "success", "pulse": "rendered pulse text"}


class _FakeSettings:
    def __init__(self, webhook_owner_user_id):
        self.webhook_owner_user_id = webhook_owner_user_id


def test_get_morning_pulse_maps_the_adk_dev_ui_default_user_to_the_configured_owner(
    monkeypatch,
):
    # adk web's stock dev UI hardcodes every session to user_id "user" with
    # no way to change it from the browser — without this fallback, local
    # testing there would always 404 on "no user record found."
    captured = {}

    def fake_render(owner_user_id):
        captured["owner_user_id"] = owner_user_id
        return "rendered pulse text"

    monkeypatch.setattr("app.tools.custom_tools.render_pulse_text_for_owner", fake_render)
    monkeypatch.setattr(
        "app.tools.custom_tools.get_settings",
        lambda: _FakeSettings(webhook_owner_user_id="usr_live_amal"),
    )

    get_morning_pulse(_FakeToolContext(user_id="user"))

    assert captured["owner_user_id"] == "usr_live_amal"


def test_get_morning_pulse_leaves_default_user_id_alone_without_a_configured_owner(
    monkeypatch,
):
    captured = {}

    def fake_render(owner_user_id):
        captured["owner_user_id"] = owner_user_id
        return "rendered pulse text"

    monkeypatch.setattr("app.tools.custom_tools.render_pulse_text_for_owner", fake_render)
    monkeypatch.setattr(
        "app.tools.custom_tools.get_settings",
        lambda: _FakeSettings(webhook_owner_user_id=""),
    )

    get_morning_pulse(_FakeToolContext(user_id="user"))

    assert captured["owner_user_id"] == "user"
