import datetime
import uuid

import pytest

from app.core.clock import FrozenClock
from app.core.models import FridayReviewDelivery
from app.ingest.seed import seed_fixture
from app.sub_agents.dossier.sub_agents.synthesize.agent import OUTPUT_KEY
from app.sub_agents.friday_review.sub_agents.synthesize.agent import (
    OUTPUT_KEY as FRIDAY_REVIEW_OUTPUT_KEY,
)
from app.triggers.slack import slack_command
from app.triggers.slack.slack_command import (
    SlashCommandPayload,
    parse_slash_command,
    resolve_owner_user_id,
    run_prep_command,
    run_pulse_command,
    run_review_command,
    run_shortlist_command,
)


@pytest.fixture(autouse=True)
def _reset_shortlist_reply_tracking():
    # module-level, process-lifetime state (see its own docstring) — leaks
    # across tests within one pytest run unless reset.
    slack_command._shortlist_reply_location.clear()
    yield


def test_parse_slash_command_extracts_the_fields_slack_sends():
    form = {
        "command": "/mentor",
        "text": "pulse",
        "user_id": "U0BN9MPRTK2",
        "channel_id": "C0BMFBNRYLA",
        "team_id": "T0IGNOREDIRRELEVANT",
    }

    payload = parse_slash_command(form)

    assert payload == SlashCommandPayload(
        command="/mentor",
        text="pulse",
        slack_user_id="U0BN9MPRTK2",
        channel_id="C0BMFBNRYLA",
    )


def test_parse_slash_command_strips_and_defaults_missing_text():
    payload = parse_slash_command(
        {"command": "/mentor", "user_id": "U1", "channel_id": "C1"}
    )

    assert payload.text == ""


def test_resolve_owner_user_id_finds_the_linked_user(pg_session, ensure_test_user):
    ensure_test_user("usr_resolve1", slack_user_id="U0RESOLVE1")

    assert resolve_owner_user_id(pg_session, "U0RESOLVE1") == "usr_resolve1"


def test_resolve_owner_user_id_returns_none_when_unlinked(pg_session):
    assert resolve_owner_user_id(pg_session, "U0NOBODY") is None


class _FakeLiveClient:
    def __init__(self, source="calendar", healthy=True, rows=None):
        self.source = source
        self._healthy = healthy
        self._rows = rows or []

    def health(self):
        from app.ingest.base import Healthy, Unauthorized

        return Healthy() if self._healthy else Unauthorized()

    def fetch(self, window, owner_user_id):
        return self._rows


def test_run_pulse_command_delivers_a_card_to_the_linked_slack_user(
    pg_session, ensure_test_user, monkeypatch
):
    ensure_test_user("usr_pulsecmd", slack_user_id="U0PULSECMD")

    monkeypatch.setattr(
        "app.triggers.slack.slack_command.seed_live",
        lambda session, owner_id, clock=None: None,
    )
    monkeypatch.setattr(
        "app.triggers.slack.slack_command.calendar_client_for", lambda session, owner_user_id: _FakeLiveClient()
    )
    monkeypatch.setattr(
        "app.triggers.slack.slack_command.LiveSlackClient", lambda **kwargs: _FakeLiveClient()
    )
    monkeypatch.setattr(
        "app.triggers.slack.slack_command.LiveLinearClient", lambda: _FakeLiveClient()
    )

    calls = []

    class _FakeDeliverer:
        def __init__(self):
            self.enabled = True

        def deliver(
            self,
            slack_user_id,
            blocks,
            channel_id=None,
            thread_ts=None,
            dm_thread_ts=None,
        ):
            calls.append((slack_user_id, channel_id))
            return {"sent": True}

    monkeypatch.setattr("app.triggers.slack.slack_command.SlackDeliverer", _FakeDeliverer)

    clock = FrozenClock(at=datetime.datetime(2026, 8, 8, 9, 0, 0))
    run_pulse_command(pg_session, "usr_pulsecmd", "C0SOMECHANNEL", clock=clock)

    assert len(calls) == 1
    assert calls[0] == ("U0PULSECMD", "C0SOMECHANNEL")


def test_run_pulse_command_delivers_audio_threaded_under_the_card(
    pg_session, ensure_test_user, monkeypatch
):
    ensure_test_user("usr_pulsecmd_audio", slack_user_id="U0PULSECMDAUDIO")

    monkeypatch.setattr(
        "app.triggers.slack.slack_command.seed_live",
        lambda session, owner_id, clock=None: None,
    )
    monkeypatch.setattr(
        "app.triggers.slack.slack_command.calendar_client_for", lambda session, owner_user_id: _FakeLiveClient()
    )
    monkeypatch.setattr(
        "app.triggers.slack.slack_command.LiveSlackClient", lambda **kwargs: _FakeLiveClient()
    )
    monkeypatch.setattr(
        "app.triggers.slack.slack_command.LiveLinearClient", lambda: _FakeLiveClient()
    )

    class _FakeDeliverer:
        def __init__(self):
            self.enabled = True

        def deliver(self, slack_user_id, blocks, channel_id=None, thread_ts=None, dm_thread_ts=None):
            return {"sent": True, "dm_ts": "999.111", "dm_channel": "D0PULSECMDAUDIO"}

    monkeypatch.setattr("app.triggers.slack.slack_command.SlackDeliverer", _FakeDeliverer)

    audio_calls = []
    monkeypatch.setattr(
        "app.triggers.slack.slack_command.deliver_pulse_audio",
        lambda deliverer, card, channel_id, thread_ts: audio_calls.append(
            (channel_id, thread_ts)
        ),
    )

    clock = FrozenClock(at=datetime.datetime(2026, 8, 8, 9, 0, 0))
    run_pulse_command(pg_session, "usr_pulsecmd_audio", None, clock=clock)

    assert audio_calls == [("D0PULSECMDAUDIO", "999.111")]


def test_run_pulse_command_skips_audio_when_delivery_has_no_dm_ts(
    pg_session, ensure_test_user, monkeypatch
):
    ensure_test_user("usr_pulsecmd_noaudio", slack_user_id="U0PULSECMDNOAUDIO")

    monkeypatch.setattr(
        "app.triggers.slack.slack_command.seed_live",
        lambda session, owner_id, clock=None: None,
    )
    monkeypatch.setattr(
        "app.triggers.slack.slack_command.calendar_client_for", lambda session, owner_user_id: _FakeLiveClient()
    )
    monkeypatch.setattr(
        "app.triggers.slack.slack_command.LiveSlackClient", lambda **kwargs: _FakeLiveClient()
    )
    monkeypatch.setattr(
        "app.triggers.slack.slack_command.LiveLinearClient", lambda: _FakeLiveClient()
    )

    class _FakeDeliverer:
        def __init__(self):
            self.enabled = True

        def deliver(self, slack_user_id, blocks, channel_id=None, thread_ts=None, dm_thread_ts=None):
            return {"sent": True}  # no dm_ts, e.g. an unexpected delivery shape

    monkeypatch.setattr("app.triggers.slack.slack_command.SlackDeliverer", _FakeDeliverer)

    audio_calls = []
    monkeypatch.setattr(
        "app.triggers.slack.slack_command.deliver_pulse_audio",
        lambda deliverer, card, channel_id, thread_ts: audio_calls.append(True),
    )

    clock = FrozenClock(at=datetime.datetime(2026, 8, 8, 9, 0, 0))
    run_pulse_command(pg_session, "usr_pulsecmd_noaudio", None, clock=clock)

    assert audio_calls == []


def test_run_pulse_command_passes_dm_thread_ts_through_to_deliver(
    pg_session, ensure_test_user, monkeypatch
):
    ensure_test_user("usr_pulsecmd_dm", slack_user_id="U0PULSECMDDM")

    monkeypatch.setattr(
        "app.triggers.slack.slack_command.seed_live",
        lambda session, owner_id, clock=None: None,
    )
    monkeypatch.setattr(
        "app.triggers.slack.slack_command.calendar_client_for", lambda session, owner_user_id: _FakeLiveClient()
    )
    monkeypatch.setattr(
        "app.triggers.slack.slack_command.LiveSlackClient", lambda **kwargs: _FakeLiveClient()
    )
    monkeypatch.setattr(
        "app.triggers.slack.slack_command.LiveLinearClient", lambda: _FakeLiveClient()
    )

    calls = []

    class _FakeDeliverer:
        def __init__(self):
            self.enabled = True

        def deliver(
            self,
            slack_user_id,
            blocks,
            channel_id=None,
            thread_ts=None,
            dm_thread_ts=None,
        ):
            calls.append((channel_id, thread_ts, dm_thread_ts))
            return {"sent": True}

    monkeypatch.setattr("app.triggers.slack.slack_command.SlackDeliverer", _FakeDeliverer)

    clock = FrozenClock(at=datetime.datetime(2026, 8, 8, 9, 0, 0))
    run_pulse_command(
        pg_session, "usr_pulsecmd_dm", None, clock=clock, dm_thread_ts="321.654"
    )

    assert calls == [(None, None, "321.654")]


def test_run_pulse_command_skips_delivery_when_slack_not_configured(
    pg_session, ensure_test_user, monkeypatch
):
    ensure_test_user("usr_pulsecmd2", slack_user_id="U0PULSECMD2")

    monkeypatch.setattr(
        "app.triggers.slack.slack_command.seed_live",
        lambda session, owner_id, clock=None: None,
    )
    monkeypatch.setattr(
        "app.triggers.slack.slack_command.calendar_client_for", lambda session, owner_user_id: _FakeLiveClient()
    )
    monkeypatch.setattr(
        "app.triggers.slack.slack_command.LiveSlackClient", lambda **kwargs: _FakeLiveClient()
    )
    monkeypatch.setattr(
        "app.triggers.slack.slack_command.LiveLinearClient", lambda: _FakeLiveClient()
    )

    class _DisabledDeliverer:
        def __init__(self):
            self.enabled = False

        def deliver(self, *args, **kwargs):
            raise AssertionError("must not deliver when disabled")

    monkeypatch.setattr("app.triggers.slack.slack_command.SlackDeliverer", _DisabledDeliverer)

    clock = FrozenClock(at=datetime.datetime(2026, 8, 8, 9, 0, 0))
    # must not raise
    run_pulse_command(pg_session, "usr_pulsecmd2", "C0SOMECHANNEL", clock=clock)


def test_run_pulse_command_still_pulses_when_seed_live_raises(
    pg_session, ensure_test_user, monkeypatch
):
    # a live-ingest hiccup (MCP server unreachable, etc.) shouldn't leave the
    # user with silence — pulse off whatever was already ingested instead.
    ensure_test_user("usr_pulsecmd3", slack_user_id="U0PULSECMD3")

    def _boom(session, owner_id, clock=None):
        raise RuntimeError("mcp spawn failed")

    monkeypatch.setattr("app.triggers.slack.slack_command.seed_live", _boom)
    monkeypatch.setattr(
        "app.triggers.slack.slack_command.calendar_client_for", lambda session, owner_user_id: _FakeLiveClient()
    )
    monkeypatch.setattr(
        "app.triggers.slack.slack_command.LiveSlackClient", lambda **kwargs: _FakeLiveClient()
    )
    monkeypatch.setattr(
        "app.triggers.slack.slack_command.LiveLinearClient", lambda: _FakeLiveClient()
    )

    calls = []

    class _FakeDeliverer:
        def __init__(self):
            self.enabled = True

        def deliver(
            self,
            slack_user_id,
            blocks,
            channel_id=None,
            thread_ts=None,
            dm_thread_ts=None,
        ):
            calls.append((slack_user_id, channel_id))
            return {"sent": True}

    monkeypatch.setattr("app.triggers.slack.slack_command.SlackDeliverer", _FakeDeliverer)

    clock = FrozenClock(at=datetime.datetime(2026, 8, 8, 9, 0, 0))
    run_pulse_command(pg_session, "usr_pulsecmd3", "C0SOMECHANNEL", clock=clock)

    assert len(calls) == 1


def _fake_run_agent_sync(agent, initial_state, kickoff_text="Begin."):
    # pull_dossier builds a real Agent internally when no llm_agent is
    # injected (Task 8's llm_agent=None fix, reused by Task 9's pull_dossier
    # — see app/sub_agents/dossier/pull.py's own docstring), which calls
    # synthesize_dossier(context, agent) -> run_agent_sync via a *local*
    # import inside synthesize_dossier, so patching must target the source
    # module, app.core.adk_runner.run_agent_sync, not any call site that
    # imports it locally (those re-resolve the name at call time).
    assert "context_json" in initial_state
    return {
        OUTPUT_KEY: {
            "who": ["Team"],
            "why_now": "Weekly standup.",
            "talking_points": [
                {"text": "Check on yesterday's blockers", "source_link": "https://linear.app/x/1"},
            ],
            "promised_and_not_delivered": [],
            "suggested_opener": None,
            "short_version": False,
        }
    }


def test_run_prep_command_delivers_a_dossier_for_the_soonest_upcoming_event(
    pg_session, ensure_test_user, monkeypatch
):
    # Per-run unique ids, not static "usr_prepcmd"/"evt-soonest": pg_session
    # (tests/unit/conftest.py) never rolls back, so a second run against the
    # same dev DB would hit dossier_deliveries' UNIQUE(owner_user_id,
    # event_external_id) constraint inside pull_dossier's commit -- caught
    # by run_prep_command's broad except Exception, so this test's own
    # assertions (checked before the DB write) kept passing while silently
    # no longer exercising the real DB write path (finding 10). Matches the
    # pattern already used in tests/unit/sub_agents/dossier/sub_agents/
    # deliver/test_agent.py and tests/unit/sub_agents/dossier/test_agent.py.
    run_id = uuid.uuid4().hex[:8]
    owner_user_id = f"usr_prepcmd_{run_id}"
    slack_user_id = f"U{run_id}"
    ensure_test_user(owner_user_id, slack_user_id=slack_user_id)
    monkeypatch.setattr("app.core.adk_runner.run_agent_sync", _fake_run_agent_sync)

    clock = FrozenClock(at=datetime.datetime(2026, 8, 19, 9, 0, 0, tzinfo=datetime.UTC))
    later_event = {
        "external_id": f"evt-later-{run_id}",
        "title": "Later meeting",
        "starts_at": "2026-08-19T15:00:00+00:00",
        "is_recurring": False,
        "attendees": [],
    }
    soonest_event = {
        "external_id": f"evt-soonest-{run_id}",
        "title": "Standup",
        "starts_at": "2026-08-19T09:15:00+00:00",
        "is_recurring": True,
        "attendees": [],
    }
    monkeypatch.setattr(
        "app.triggers.slack.slack_command.calendar_client_for",
        lambda session, owner_user_id: _FakeLiveClient(rows=[later_event, soonest_event]),
    )
    monkeypatch.setattr(
        "app.triggers.slack.slack_command.LiveSlackClient", lambda **kwargs: _FakeLiveClient()
    )
    monkeypatch.setattr(
        "app.triggers.slack.slack_command.LiveLinearClient", lambda: _FakeLiveClient()
    )

    calls = []

    class _FakeDeliverer:
        def __init__(self):
            self.enabled = True

        def deliver(
            self,
            slack_user_id,
            blocks,
            channel_id=None,
            thread_ts=None,
            dm_thread_ts=None,
        ):
            calls.append((slack_user_id, channel_id))
            return {"sent": True}

        def send_text_message(self, channel_id, text):
            raise AssertionError("must not send a no-event message when an event exists")

    monkeypatch.setattr("app.triggers.slack.slack_command.SlackDeliverer", _FakeDeliverer)

    run_prep_command(pg_session, owner_user_id, clock=clock)

    assert len(calls) == 1
    assert calls[0] == (slack_user_id, None)


def test_run_prep_command_says_so_when_no_upcoming_event(
    pg_session, ensure_test_user, monkeypatch
):
    ensure_test_user("usr_prepcmd_none", slack_user_id="U0PREPCMDNONE")

    clock = FrozenClock(at=datetime.datetime(2026, 8, 19, 9, 0, 0, tzinfo=datetime.UTC))
    monkeypatch.setattr(
        "app.triggers.slack.slack_command.calendar_client_for", lambda session, owner_user_id: _FakeLiveClient(rows=[])
    )

    messages = []

    class _FakeDeliverer:
        def __init__(self):
            self.enabled = True

        def send_text_message(self, channel_id, text):
            messages.append((channel_id, text))

        def deliver(self, *args, **kwargs):
            raise AssertionError("must not deliver a card when there's no event")

    monkeypatch.setattr("app.triggers.slack.slack_command.SlackDeliverer", _FakeDeliverer)

    run_prep_command(pg_session, "usr_prepcmd_none", clock=clock)

    assert len(messages) == 1
    assert messages[0][0] == "U0PREPCMDNONE"


def test_run_prep_command_skips_when_slack_not_configured(
    pg_session, ensure_test_user, monkeypatch
):
    ensure_test_user("usr_prepcmd_noslack", slack_user_id="U0PREPCMDNOSLACK")

    clock = FrozenClock(at=datetime.datetime(2026, 8, 19, 9, 0, 0, tzinfo=datetime.UTC))
    monkeypatch.setattr(
        "app.triggers.slack.slack_command.calendar_client_for", lambda session, owner_user_id: _FakeLiveClient(rows=[])
    )

    class _DisabledDeliverer:
        def __init__(self):
            self.enabled = False

        def send_text_message(self, *args, **kwargs):
            raise AssertionError("must not message when disabled")

        def deliver(self, *args, **kwargs):
            raise AssertionError("must not deliver when disabled")

    monkeypatch.setattr("app.triggers.slack.slack_command.SlackDeliverer", _DisabledDeliverer)

    # must not raise
    run_prep_command(pg_session, "usr_prepcmd_noslack", clock=clock)


def test_run_review_command_delivers(pg_session, ensure_test_user, monkeypatch):
    run_id = uuid.uuid4().hex[:8]
    owner_user_id = f"usr_reviewcmd_{run_id}"
    slack_user_id = f"U{run_id}"
    ensure_test_user(owner_user_id, slack_user_id=slack_user_id)

    def _fake_run_agent_sync(agent, initial_state, kickoff_text="Begin."):
        return {
            FRIDAY_REVIEW_OUTPUT_KEY: {
                "wins": [],
                "one_adjustment": None,
                "career_narrative": None,
                "skill_classifications": [],
            }
        }

    monkeypatch.setattr("app.core.adk_runner.run_agent_sync", _fake_run_agent_sync)

    calls = []

    class _FakeDeliverer:
        def __init__(self):
            self.enabled = True

        def deliver(self, slack_user_id, blocks, channel_id=None, thread_ts=None, dm_thread_ts=None):
            calls.append((slack_user_id, channel_id))
            return {"sent": True, "dm_ts": "1.1", "dm_channel": "D1"}

    monkeypatch.setattr("app.triggers.slack.slack_command.SlackDeliverer", _FakeDeliverer)

    clock = FrozenClock(at=datetime.datetime(2026, 8, 25, 10, 0, 0, tzinfo=datetime.UTC))
    run_review_command(pg_session, owner_user_id, clock=clock)

    assert len(calls) == 1
    assert calls[0] == (slack_user_id, None)

    delivery = (
        pg_session.query(FridayReviewDelivery)
        .filter(FridayReviewDelivery.owner_user_id == owner_user_id)
        .one()
    )
    assert delivery.trigger == "pull"
    assert delivery.sent_at is not None


def _clock_for(fixture):
    return FrozenClock(at=datetime.datetime.fromisoformat(fixture["now"]))


def test_run_shortlist_command_posts_every_shortlist_item_threaded_in_the_dm(
    pg_session, monkeypatch
):
    from app.core.models import User
    from app.ingest.fixture_source import load_day_fixture

    fixture = load_day_fixture("normal_day")
    counts = seed_fixture(pg_session, "normal_day")
    owner_user_id = counts["owner_user_id"]
    # normal_day.yaml's owner id is also used for real live testing in this
    # shared dev DB — read its existing slack_user_id rather than
    # overwriting it, so this test can't clobber a real link (a prior
    # version of this test did exactly that, for real, mid-session).
    expected_slack_user_id = pg_session.get(User, owner_user_id).slack_user_id

    calls = []

    class _FakeDeliverer:
        def __init__(self):
            self.enabled = True

        def post_thread_reply(self, channel_id, blocks, text, thread_ts=None):
            calls.append((channel_id, thread_ts, blocks))
            return {"sent": True}

    monkeypatch.setattr("app.triggers.slack.slack_command.SlackDeliverer", _FakeDeliverer)

    run_shortlist_command(
        pg_session, owner_user_id, dm_thread_ts="42.1", clock=_clock_for(fixture)
    )

    assert len(calls) == 1
    channel_id, thread_ts, blocks = calls[0]
    assert channel_id == expected_slack_user_id
    assert thread_ts == "42.1"
    # normal_day: 6 shortlist candidates (incl. Sarah's DM) — every one
    # should be listed, not just the 1-3 the ranker would cut to.
    section_texts = [b["text"]["text"] for b in blocks if b["type"] == "section"]
    assert len(section_texts) == 7  # header + 6 items


def test_run_shortlist_command_skips_delivery_when_slack_not_configured(
    pg_session, monkeypatch
):
    from app.ingest.fixture_source import load_day_fixture

    fixture = load_day_fixture("normal_day")
    counts = seed_fixture(pg_session, "normal_day")

    class _DisabledDeliverer:
        def __init__(self):
            self.enabled = False

        def post_thread_reply(self, *args, **kwargs):
            raise AssertionError("must not deliver when disabled")

    monkeypatch.setattr("app.triggers.slack.slack_command.SlackDeliverer", _DisabledDeliverer)

    # must not raise
    run_shortlist_command(
        pg_session, counts["owner_user_id"], clock=_clock_for(fixture)
    )


def test_run_shortlist_command_repeat_click_reacts_instead_of_reposting(
    pg_session, monkeypatch
):
    from app.ingest.fixture_source import load_day_fixture

    fixture_ = load_day_fixture("normal_day")
    counts = seed_fixture(pg_session, "normal_day")
    owner_user_id = counts["owner_user_id"]

    calls = []

    class _FakeDeliverer:
        def __init__(self):
            self.enabled = True

        def post_thread_reply(self, channel_id, blocks, text, thread_ts=None):
            calls.append(("post_thread_reply",))
            return {"sent": True, "ts": "999.111", "channel": "D_RESOLVED"}

        def add_reaction(self, channel, ts, emoji):
            calls.append(("add_reaction", channel, ts, emoji))

    monkeypatch.setattr("app.triggers.slack.slack_command.SlackDeliverer", _FakeDeliverer)

    run_shortlist_command(
        pg_session, owner_user_id, dm_thread_ts="42.1", clock=_clock_for(fixture_)
    )
    run_shortlist_command(
        pg_session, owner_user_id, dm_thread_ts="42.1", clock=_clock_for(fixture_)
    )

    assert calls == [
        ("post_thread_reply",),
        ("add_reaction", "D_RESOLVED", "999.111", "eyes"),
    ]


def test_run_shortlist_command_different_cards_each_get_their_own_reply(
    pg_session, monkeypatch
):
    from app.ingest.fixture_source import load_day_fixture

    fixture_ = load_day_fixture("normal_day")
    counts = seed_fixture(pg_session, "normal_day")
    owner_user_id = counts["owner_user_id"]

    calls = []

    class _FakeDeliverer:
        def __init__(self):
            self.enabled = True

        def post_thread_reply(self, channel_id, blocks, text, thread_ts=None):
            calls.append(("post_thread_reply", thread_ts))
            return {"sent": True, "ts": "999.111", "channel": "D_RESOLVED"}

        def add_reaction(self, channel, ts, emoji):
            calls.append(("add_reaction", channel, ts, emoji))

    monkeypatch.setattr("app.triggers.slack.slack_command.SlackDeliverer", _FakeDeliverer)

    run_shortlist_command(
        pg_session, owner_user_id, dm_thread_ts="42.1", clock=_clock_for(fixture_)
    )
    run_shortlist_command(
        pg_session, owner_user_id, dm_thread_ts="99.9", clock=_clock_for(fixture_)
    )

    assert calls == [
        ("post_thread_reply", "42.1"),
        ("post_thread_reply", "99.9"),
    ]
