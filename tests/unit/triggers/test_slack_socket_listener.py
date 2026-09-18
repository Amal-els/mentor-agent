import pytest
from sqlalchemy import select

from app.delivery.cards import (
    FEEDBACK_DOWN_ACTION_ID,
    FEEDBACK_UP_ACTION_ID,
    FRIDAY_REVIEW_CONFIRM_LOG_ACTION_ID,
    SHOW_FULL_SHORTLIST_ACTION_ID,
)
from app.core.models import Message
from app.core.scope import OwnerScope
from app.triggers.slack import slack_socket_listener
from app.triggers.slack.slack_socket_listener import (
    _AgendaMentionContext,
    _persist_slack_message_with_own_session,
    handle_socket_mode_request,
)


@pytest.fixture(autouse=True)
def _reset_event_dedup_cache():
    # the module-level dedup cache is deliberately process-lifetime state
    # (see its docstring) but that leaks across tests within one pytest run
    # unless reset.
    slack_socket_listener._seen_event_ids.clear()
    yield


class _FakeReq:
    def __init__(self, type, envelope_id, payload):
        self.type = type
        self.envelope_id = envelope_id
        self.payload = payload


class _FakeClient:
    def __init__(self):
        self.acks = []

    def send_socket_mode_response(self, response):
        self.acks.append(response.envelope_id)


class _FakeSessionFactory:
    """Mimics sessionmaker() — a callable returning a fresh session-like
    object each time, matching how handle_socket_mode_request uses it
    (short-lived lookups + a longer-lived background job)."""

    def __init__(self, session):
        self._session = session

    def __call__(self):
        return self._session


class _FakeDeliverer:
    calls: list

    def __init__(self):
        self.__class__.calls = getattr(self.__class__, "calls", [])

    def post_channel_message(self, channel_id, text):
        self.__class__.calls.append(("post_channel_message", channel_id, text))
        return {"sent": True, "ts": "999.888"}

    def send_text_message(self, channel_id, text):
        self.__class__.calls.append(("send_text_message", channel_id, text))


def test_every_envelope_is_acked_immediately_regardless_of_type():
    client = _FakeClient()
    req = _FakeReq("hello", "env-1", {})

    handle_socket_mode_request(client, req, _FakeSessionFactory(session=None))

    assert client.acks == ["env-1"]


def test_non_slash_command_envelopes_are_acked_and_otherwise_ignored():
    client = _FakeClient()
    req = _FakeReq("events_api", "env-2", {"event": {"type": "message"}})

    # must not raise even though session_factory would blow up if called
    handle_socket_mode_request(client, req, session_factory=None)

    assert client.acks == ["env-2"]


def test_unsupported_subcommand_replies_via_response_url(monkeypatch):
    client = _FakeClient()
    posted = []
    monkeypatch.setattr(
        "app.triggers.slack.slack_socket_listener.requests.post",
        lambda url, json, timeout: posted.append((url, json)),
    )

    req = _FakeReq(
        "slash_commands",
        "env-3",
        {
            "command": "/mentor",
            "text": "quiet",
            "user_id": "U1",
            "channel_id": "C1",
            "response_url": "https://hooks.slack.com/commands/fake",
        },
    )

    handle_socket_mode_request(client, req, session_factory=None)

    assert client.acks == ["env-3"]
    assert len(posted) == 1
    assert posted[0][0] == "https://hooks.slack.com/commands/fake"
    assert (
        "not supported" in posted[0][1]["text"].lower()
        or "isn't supported" in posted[0][1]["text"]
    )


def test_unlinked_user_replies_via_response_url(monkeypatch, pg_session):
    client = _FakeClient()
    posted = []
    monkeypatch.setattr(
        "app.triggers.slack.slack_socket_listener.requests.post",
        lambda url, json, timeout: posted.append((url, json)),
    )

    req = _FakeReq(
        "slash_commands",
        "env-4",
        {
            "command": "/mentor",
            "text": "pulse",
            "user_id": "U0NOBODYLINKED",
            "channel_id": "C1",
            "response_url": "https://hooks.slack.com/commands/fake",
        },
    )

    handle_socket_mode_request(
        client, req, session_factory=_FakeSessionFactory(session=pg_session)
    )

    assert client.acks == ["env-4"]
    assert len(posted) == 1
    assert "isn't linked" in posted[0][1]["text"]


def test_linked_user_in_a_channel_gets_a_progress_message_and_schedules_the_job(
    monkeypatch, pg_session, ensure_test_user
):
    ensure_test_user("usr_socket1", slack_user_id="U0SOCKET1")
    _FakeDeliverer.calls = []
    monkeypatch.setattr(
        "app.triggers.slack.slack_socket_listener.SlackDeliverer", _FakeDeliverer
    )

    client = _FakeClient()
    scheduled = []
    monkeypatch.setattr(
        "app.triggers.slack.slack_socket_listener._schedule",
        lambda target, *args: scheduled.append((target, args)),
    )

    req = _FakeReq(
        "slash_commands",
        "env-5",
        {
            "command": "/mentor",
            "text": "pulse",
            "user_id": "U0SOCKET1",
            "channel_id": "C0SOCKET1",
            "response_url": "https://hooks.slack.com/commands/fake",
        },
    )

    handle_socket_mode_request(
        client, req, session_factory=_FakeSessionFactory(session=pg_session)
    )

    assert client.acks == ["env-5"]
    assert len(scheduled) == 1
    target, args = scheduled[0]
    assert target.__name__ == "_run_pulse_command_with_own_session"
    # (owner_user_id, channel_id, thread_ts, dm_thread_ts) — thread_ts
    # threads the later "sent you that in DM" ack under the ack message;
    # dm_thread_ts is None since this was channel-originated.
    assert args[1:] == ("usr_socket1", "C0SOCKET1", "999.888", None)

    assert _FakeDeliverer.calls == [
        (
            "post_channel_message",
            "C0SOCKET1",
            "<@U0SOCKET1> 🎯 Got it — pulling your pulse together...",
        )
    ]


def test_prep_command_acks_via_response_url_and_schedules_the_job(
    monkeypatch, pg_session, ensure_test_user
):
    # Unlike pulse, prep needs no channel-vs-DM ack branching -- a dossier
    # is always DM-only (deliver_dossier never threads a channel_id
    # through) -- so this just replies via response_url and schedules the
    # background job directly, matching slack_router.py's HTTP-path twin.
    ensure_test_user("usr_socket_prep", slack_user_id="U0SOCKETPREP")

    client = _FakeClient()
    posted = []
    monkeypatch.setattr(
        "app.triggers.slack.slack_socket_listener.requests.post",
        lambda url, json, timeout: posted.append((url, json)),
    )
    scheduled = []
    monkeypatch.setattr(
        "app.triggers.slack.slack_socket_listener._schedule",
        lambda target, *args: scheduled.append((target, args)),
    )

    req = _FakeReq(
        "slash_commands",
        "env-prep-1",
        {
            "command": "/mentor",
            "text": "prep",
            "user_id": "U0SOCKETPREP",
            "channel_id": "C0SOCKETPREP",
            "response_url": "https://hooks.slack.com/commands/fake",
        },
    )

    handle_socket_mode_request(
        client, req, session_factory=_FakeSessionFactory(session=pg_session)
    )

    assert client.acks == ["env-prep-1"]
    assert len(posted) == 1
    assert posted[0][0] == "https://hooks.slack.com/commands/fake"
    assert "dossier is on its way" in posted[0][1]["text"]

    assert len(scheduled) == 1
    target, args = scheduled[0]
    assert target.__name__ == "_run_prep_command_with_own_session"
    assert args[1:] == ("usr_socket_prep",)


def test_review_command_acks_via_response_url_and_schedules_the_job(
    monkeypatch, pg_session, ensure_test_user
):
    # Same shape as the "prep" branch — a Friday reflection is DM-only by
    # construction (pull_friday_review never threads a channel_id through)
    # so this just replies via response_url and schedules the job.
    ensure_test_user("usr_socket_review", slack_user_id="U0SOCKETREVIEW")

    client = _FakeClient()
    posted = []
    monkeypatch.setattr(
        "app.triggers.slack.slack_socket_listener.requests.post",
        lambda url, json, timeout: posted.append((url, json)),
    )
    scheduled = []
    monkeypatch.setattr(
        "app.triggers.slack.slack_socket_listener._schedule",
        lambda target, *args: scheduled.append((target, args)),
    )

    req = _FakeReq(
        "slash_commands",
        "env-review-1",
        {
            "command": "/mentor",
            "text": "review",
            "user_id": "U0SOCKETREVIEW",
            "channel_id": "C0SOCKETREVIEW",
            "response_url": "https://hooks.slack.com/commands/fake",
        },
    )

    handle_socket_mode_request(
        client, req, session_factory=_FakeSessionFactory(session=pg_session)
    )

    assert client.acks == ["env-review-1"]
    assert len(posted) == 1
    assert posted[0][0] == "https://hooks.slack.com/commands/fake"
    assert "reflection is on its way" in posted[0][1]["text"]

    assert len(scheduled) == 1
    target, args = scheduled[0]
    assert target.__name__ == "_run_review_command_with_own_session"
    assert args[1:] == ("usr_socket_review",)


def test_linked_user_in_a_dm_slash_command_gets_the_ack_posted_in_the_dm(
    monkeypatch, pg_session, ensure_test_user
):
    # channel_id starting with D means this was invoked from the bot's own
    # DM — the ack is posted directly in the DM (not skipped), and the
    # card itself threads under it (dm_thread_ts), not a channel-side ack.
    ensure_test_user("usr_socket_dm", slack_user_id="U0SOCKETDM")
    _FakeDeliverer.calls = []
    monkeypatch.setattr(
        "app.triggers.slack.slack_socket_listener.SlackDeliverer", _FakeDeliverer
    )

    client = _FakeClient()
    scheduled = []
    monkeypatch.setattr(
        "app.triggers.slack.slack_socket_listener._schedule",
        lambda target, *args: scheduled.append((target, args)),
    )

    req = _FakeReq(
        "slash_commands",
        "env-5b",
        {
            "command": "/mentor",
            "text": "pulse",
            "user_id": "U0SOCKETDM",
            "channel_id": "D0SOCKETDM",
            "response_url": "https://hooks.slack.com/commands/fake",
        },
    )

    handle_socket_mode_request(
        client, req, session_factory=_FakeSessionFactory(session=pg_session)
    )

    assert len(scheduled) == 1
    _target, args = scheduled[0]
    # (owner_user_id, channel_id, thread_ts, dm_thread_ts) — channel_id and
    # thread_ts are None (no separate channel), dm_thread_ts threads the
    # card under the ack, in the same DM.
    assert args[1:] == ("usr_socket_dm", None, None, "999.888")
    assert _FakeDeliverer.calls == [
        (
            "post_channel_message",
            "D0SOCKETDM",
            "🎯 Got it — pulling your pulse together...",
        )
    ]


def test_reply_logs_rather_than_raises_when_response_url_missing(monkeypatch):
    # a defensive edge case — Slack always includes response_url on real
    # slash-command payloads, but nothing should crash if it's absent
    client = _FakeClient()
    req = _FakeReq(
        "slash_commands",
        "env-6",
        {"command": "/mentor", "text": "quiet", "user_id": "U1", "channel_id": "C1"},
    )

    handle_socket_mode_request(client, req, session_factory=None)

    assert client.acks == ["env-6"]


def _dm_message_req(
    envelope_id, text, user="U0DM1", channel="D0DM1", event_id="Ev1", **extra
):
    event = {
        "type": "message",
        "channel_type": "im",
        "channel": channel,
        "user": user,
        "text": text,
        "ts": "111.222",
        **extra,
    }
    return _FakeReq("events_api", envelope_id, {"event_id": event_id, "event": event})


def test_dm_message_matching_pulse_alias_posts_ack_and_schedules_job(
    monkeypatch, pg_session, ensure_test_user
):
    ensure_test_user("usr_dm1", slack_user_id="U0DM1")
    _FakeDeliverer.calls = []
    monkeypatch.setattr(
        "app.triggers.slack.slack_socket_listener.SlackDeliverer", _FakeDeliverer
    )
    scheduled = []
    monkeypatch.setattr(
        "app.triggers.slack.slack_socket_listener._schedule",
        lambda target, *args: scheduled.append((target, args)),
    )

    client = _FakeClient()
    req = _dm_message_req("env-dm1", "what's my pulse today?")

    handle_socket_mode_request(
        client, req, session_factory=_FakeSessionFactory(session=pg_session)
    )

    assert client.acks == ["env-dm1"]
    assert _FakeDeliverer.calls == [
        (
            "post_channel_message",
            "D0DM1",
            "🎯 Got it — pulling your pulse together...",
        )
    ]
    # Two independent jobs now: real-time message persistence (always, for
    # any DM from a linked owner) and the pulse-alias trigger (only
    # because this message happens to match).
    assert len(scheduled) == 2
    persist_target, persist_args = scheduled[0]
    assert persist_target.__name__ == "_persist_slack_message_with_own_session"
    assert persist_args[1] == "usr_dm1"

    pulse_target, pulse_args = scheduled[1]
    assert pulse_target.__name__ == "_run_pulse_command_with_own_session"
    # (owner_user_id, channel_id, thread_ts, dm_thread_ts) — same
    # DM-originated shape as the slash-command-in-DM path.
    assert pulse_args[1:] == ("usr_dm1", None, None, "999.888")


def test_dm_message_not_matching_any_alias_from_an_unlinked_user_is_ignored(
    pg_session,
):
    # an unlinked sender can't be attributed to any owner, so nothing gets
    # persisted or scheduled — a fresh user id keeps this independent of
    # whatever other tests in this file may have already linked "U0DM1" to.
    client = _FakeClient()
    req = _dm_message_req(
        "env-dm2", "can you help me write an email", user="U0NOBODYDM2"
    )

    handle_socket_mode_request(
        client, req, session_factory=_FakeSessionFactory(session=pg_session)
    )

    assert client.acks == ["env-dm2"]


def test_dm_message_not_matching_any_alias_from_a_linked_user_is_still_persisted(
    monkeypatch, pg_session, ensure_test_user
):
    # real-time persistence doesn't depend on the message looking like a
    # pulse request — every DM from a linked owner gets saved.
    ensure_test_user("usr_dm9", slack_user_id="U0DM9")
    scheduled = []
    monkeypatch.setattr(
        "app.triggers.slack.slack_socket_listener._schedule",
        lambda target, *args: scheduled.append((target, args)),
    )

    client = _FakeClient()
    req = _dm_message_req(
        "env-dm2b", "can you help me write an email", user="U0DM9"
    )

    handle_socket_mode_request(
        client, req, session_factory=_FakeSessionFactory(session=pg_session)
    )

    assert client.acks == ["env-dm2b"]
    assert len(scheduled) == 1
    target, args = scheduled[0]
    assert target.__name__ == "_persist_slack_message_with_own_session"
    assert args[1] == "usr_dm9"


def test_dm_message_mentioning_another_scheduled_user_persists_to_them_not_sender(
    monkeypatch, pg_session, ensure_test_user
):
    # DAG DMs the bot naming Amal ("@amal I'm done fixing X") — that's a
    # signal for AMAL to act on, not an actionable item for DAG, who
    # already did his part by sending it. Real bug found live: this used
    # to persist unconditionally under the sender's own scope.
    ensure_test_user("usr_dag", slack_user_id="U0DAG")
    ensure_test_user("usr_amal", slack_user_id="U0AMAL")
    monkeypatch.setenv("PULSE_SCHEDULED_OWNER_IDS", "usr_dag,usr_amal")
    scheduled = []
    monkeypatch.setattr(
        "app.triggers.slack.slack_socket_listener._schedule",
        lambda target, *args: scheduled.append((target, args)),
    )

    client = _FakeClient()
    req = _dm_message_req(
        "env-dm-mention1",
        "<@U0AMAL> I'm done fixing the login page, review whenever",
        user="U0DAG",
    )

    handle_socket_mode_request(
        client, req, session_factory=_FakeSessionFactory(session=pg_session)
    )

    assert client.acks == ["env-dm-mention1"]
    assert len(scheduled) == 1
    target, args = scheduled[0]
    assert target.__name__ == "_persist_slack_message_with_own_session"
    assert args[1] == "usr_amal"


def test_dm_message_mentioning_no_one_else_still_persists_to_sender(
    monkeypatch, pg_session, ensure_test_user
):
    # Baseline unchanged even with PULSE_SCHEDULED_OWNER_IDS configured:
    # a DM that doesn't name another scheduled user is still "about the
    # sender's own stuff," so it stays with them.
    ensure_test_user("usr_dag2", slack_user_id="U0DAG2")
    ensure_test_user("usr_amal2", slack_user_id="U0AMAL2")
    monkeypatch.setenv("PULSE_SCHEDULED_OWNER_IDS", "usr_dag2,usr_amal2")
    scheduled = []
    monkeypatch.setattr(
        "app.triggers.slack.slack_socket_listener._schedule",
        lambda target, *args: scheduled.append((target, args)),
    )

    client = _FakeClient()
    req = _dm_message_req(
        "env-dm-mention2", "need to reply to Bob about the deploy", user="U0DAG2"
    )

    handle_socket_mode_request(
        client, req, session_factory=_FakeSessionFactory(session=pg_session)
    )

    assert len(scheduled) == 1
    target, args = scheduled[0]
    assert target.__name__ == "_persist_slack_message_with_own_session"
    assert args[1] == "usr_dag2"


def test_dm_message_mentioning_two_scheduled_users_persists_to_both(
    monkeypatch, pg_session, ensure_test_user
):
    ensure_test_user("usr_dag3", slack_user_id="U0DAG3")
    ensure_test_user("usr_amal3", slack_user_id="U0AMAL3")
    ensure_test_user("usr_bob3", slack_user_id="U0BOB3")
    monkeypatch.setenv("PULSE_SCHEDULED_OWNER_IDS", "usr_dag3,usr_amal3,usr_bob3")
    scheduled = []
    monkeypatch.setattr(
        "app.triggers.slack.slack_socket_listener._schedule",
        lambda target, *args: scheduled.append((target, args)),
    )

    client = _FakeClient()
    req = _dm_message_req(
        "env-dm-mention3",
        "<@U0AMAL3> <@U0BOB3> both of you should look at this",
        user="U0DAG3",
    )

    handle_socket_mode_request(
        client, req, session_factory=_FakeSessionFactory(session=pg_session)
    )

    assert len(scheduled) == 2
    persisted_owner_ids = {args[1] for _target, args in scheduled}
    assert persisted_owner_ids == {"usr_amal3", "usr_bob3"}


def test_dm_message_from_the_bot_itself_is_ignored():
    client = _FakeClient()
    req = _dm_message_req("env-dm3", "pulse", bot_id="B0SELF")

    # must not raise even though session_factory would blow up if called
    handle_socket_mode_request(client, req, session_factory=None)

    assert client.acks == ["env-dm3"]


def test_dm_message_edit_subtype_is_ignored():
    client = _FakeClient()
    req = _dm_message_req("env-dm4", "pulse", subtype="message_changed")

    handle_socket_mode_request(client, req, session_factory=None)

    assert client.acks == ["env-dm4"]


def test_channel_message_without_a_configured_owner_is_ignored(monkeypatch):
    # no PULSE_SCHEDULED_OWNER_IDS configured — must bail before ever
    # touching session_factory, which would blow up if called with None
    # here.
    monkeypatch.delenv("PULSE_SCHEDULED_OWNER_IDS", raising=False)

    client = _FakeClient()
    req = _dm_message_req(
        "env-dm5", "<@U0CHANOWNER> can you review this?", channel="C0SOMECHANNEL"
    )
    req.payload["event"]["channel_type"] = "channel"

    handle_socket_mode_request(client, req, session_factory=None)

    assert client.acks == ["env-dm5"]


def test_channel_message_not_mentioning_the_owner_is_ignored(
    monkeypatch, pg_session, ensure_test_user
):
    ensure_test_user("usr_chan1", slack_user_id="U0CHANOWNER1")
    scheduled = []
    monkeypatch.setattr(
        "app.triggers.slack.slack_socket_listener._schedule",
        lambda target, *args: scheduled.append((target, args)),
    )

    monkeypatch.setenv("PULSE_SCHEDULED_OWNER_IDS", "usr_chan1")

    client = _FakeClient()
    req = _dm_message_req(
        "env-dm5", "unrelated chatter", channel="C0SOMECHANNEL", user="U0SOMEONEELSE"
    )
    req.payload["event"]["channel_type"] = "channel"

    handle_socket_mode_request(
        client, req, session_factory=_FakeSessionFactory(session=pg_session)
    )

    assert client.acks == ["env-dm5"]
    assert scheduled == []


def test_channel_message_mentioning_the_owner_is_persisted(
    monkeypatch, pg_session, ensure_test_user
):
    ensure_test_user("usr_chan2", slack_user_id="U0CHANOWNER2")
    scheduled = []
    monkeypatch.setattr(
        "app.triggers.slack.slack_socket_listener._schedule",
        lambda target, *args: scheduled.append((target, args)),
    )

    monkeypatch.setenv("PULSE_SCHEDULED_OWNER_IDS", "usr_chan2")

    client = _FakeClient()
    req = _dm_message_req(
        "env-dm5",
        "<@U0CHANOWNER2> can you review this?",
        channel="C0SOMECHANNEL",
        user="U0SOMEONEELSE",
    )
    req.payload["event"]["channel_type"] = "channel"

    handle_socket_mode_request(
        client, req, session_factory=_FakeSessionFactory(session=pg_session)
    )

    assert client.acks == ["env-dm5"]
    assert len(scheduled) == 1
    target, args = scheduled[0]
    assert target.__name__ == "_persist_slack_message_with_own_session"
    assert args[1] == "usr_chan2"
    # No Pair fixture here, so no manager to resolve, and the sender
    # ("U0SOMEONEELSE") isn't a linked user — third-party mention of the
    # report only. pair_contexts is a list[tuple[report_user_id,
    # _AgendaMentionContext]] — usr_chan2 has no active Pairs, so this
    # mention is only covered by the directly-mentioned fallback, which
    # carries no Pair context at all: an empty list, not a bare context.
    pair_contexts = args[4]
    assert pair_contexts == []


def test_dm_message_unlinked_user_gets_a_text_reply_no_reaction(
    monkeypatch, pg_session
):
    _FakeDeliverer.calls = []
    monkeypatch.setattr(
        "app.triggers.slack.slack_socket_listener.SlackDeliverer", _FakeDeliverer
    )

    client = _FakeClient()
    req = _dm_message_req("env-dm6", "pulse", user="U0NOBODYLINKEDDM")

    handle_socket_mode_request(
        client, req, session_factory=_FakeSessionFactory(session=pg_session)
    )

    assert client.acks == ["env-dm6"]
    kinds = [call[0] for call in _FakeDeliverer.calls]
    assert kinds == ["send_text_message"]
    assert "isn't linked" in _FakeDeliverer.calls[0][2]


def test_dm_message_duplicate_event_id_is_only_handled_once(
    monkeypatch, pg_session, ensure_test_user
):
    ensure_test_user("usr_dm2", slack_user_id="U0DM2")
    _FakeDeliverer.calls = []
    monkeypatch.setattr(
        "app.triggers.slack.slack_socket_listener.SlackDeliverer", _FakeDeliverer
    )
    scheduled = []
    monkeypatch.setattr(
        "app.triggers.slack.slack_socket_listener._schedule",
        lambda target, *args: scheduled.append((target, args)),
    )

    client = _FakeClient()
    req1 = _dm_message_req("env-dm7", "pulse", user="U0DM2", event_id="Ev-dup")
    req2 = _dm_message_req("env-dm8", "pulse", user="U0DM2", event_id="Ev-dup")

    session_factory = _FakeSessionFactory(session=pg_session)
    handle_socket_mode_request(client, req1, session_factory=session_factory)
    handle_socket_mode_request(client, req2, session_factory=session_factory)

    assert client.acks == ["env-dm7", "env-dm8"]  # both acked
    # only handled once — 2 jobs (persist + pulse trigger), not 4
    assert len(scheduled) == 2


def _interactive_req(envelope_id, action_id, user="U0INT1", channel="D0INT1", value=None):
    action = {"action_id": action_id}
    if value is not None:
        action["value"] = value
    payload = {
        "type": "block_actions",
        "user": {"id": user},
        "channel": {"id": channel},
        "message": {"ts": "500.1"},
        "actions": [action],
    }
    return _FakeReq("interactive", envelope_id, payload)


def test_show_full_shortlist_button_click_schedules_the_job(
    monkeypatch, pg_session, ensure_test_user
):
    ensure_test_user("usr_int1", slack_user_id="U0INT1")
    scheduled = []
    monkeypatch.setattr(
        "app.triggers.slack.slack_socket_listener._schedule",
        lambda target, *args: scheduled.append((target, args)),
    )

    client = _FakeClient()
    req = _interactive_req("env-int1", SHOW_FULL_SHORTLIST_ACTION_ID)

    handle_socket_mode_request(
        client, req, session_factory=_FakeSessionFactory(session=pg_session)
    )

    assert client.acks == ["env-int1"]
    assert len(scheduled) == 1
    target, args = scheduled[0]
    assert target.__name__ == "_run_shortlist_command_with_own_session"
    assert args[1:] == ("usr_int1", "500.1")


def test_unrelated_action_id_is_ignored(monkeypatch, pg_session, ensure_test_user):
    ensure_test_user("usr_int2", slack_user_id="U0INT2")
    scheduled = []
    monkeypatch.setattr(
        "app.triggers.slack.slack_socket_listener._schedule",
        lambda target, *args: scheduled.append((target, args)),
    )

    client = _FakeClient()
    req = _interactive_req("env-int2", "some_other_button", user="U0INT2")

    handle_socket_mode_request(
        client, req, session_factory=_FakeSessionFactory(session=pg_session)
    )

    assert client.acks == ["env-int2"]
    assert scheduled == []


def test_show_full_shortlist_click_from_an_unlinked_user_is_ignored(pg_session):
    client = _FakeClient()
    req = _interactive_req(
        "env-int3", SHOW_FULL_SHORTLIST_ACTION_ID, user="U0NOBODYINT"
    )

    # must not raise
    handle_socket_mode_request(
        client, req, session_factory=_FakeSessionFactory(session=pg_session)
    )

    assert client.acks == ["env-int3"]


def test_feedback_up_click_schedules_record_feedback(
    monkeypatch, pg_session, ensure_test_user
):
    ensure_test_user("usr_int4", slack_user_id="U0INT4")
    scheduled = []
    monkeypatch.setattr(
        "app.triggers.slack.slack_socket_listener._schedule",
        lambda target, *args: scheduled.append((target, args)),
    )

    client = _FakeClient()
    req = _interactive_req(
        "env-int4",
        FEEDBACK_UP_ACTION_ID,
        user="U0INT4",
        value="pd-1::event::evt-1",
    )

    handle_socket_mode_request(
        client, req, session_factory=_FakeSessionFactory(session=pg_session)
    )

    assert client.acks == ["env-int4"]
    assert len(scheduled) == 1
    target, args = scheduled[0]
    assert target.__name__ == "_run_feedback_command_with_own_session"
    assert args[1:] == ("usr_int4", "pd-1", "evt-1", "event", "up")


def test_feedback_down_click_schedules_record_feedback(
    monkeypatch, pg_session, ensure_test_user
):
    ensure_test_user("usr_int5", slack_user_id="U0INT5")
    scheduled = []
    monkeypatch.setattr(
        "app.triggers.slack.slack_socket_listener._schedule",
        lambda target, *args: scheduled.append((target, args)),
    )

    client = _FakeClient()
    req = _interactive_req(
        "env-int5",
        FEEDBACK_DOWN_ACTION_ID,
        user="U0INT5",
        value="pd-2::work_item::wi-1",
    )

    handle_socket_mode_request(
        client, req, session_factory=_FakeSessionFactory(session=pg_session)
    )

    assert len(scheduled) == 1
    target, args = scheduled[0]
    assert target.__name__ == "_run_feedback_command_with_own_session"
    assert args[1:] == ("usr_int5", "pd-2", "wi-1", "work_item", "down")


def test_feedback_click_from_an_unlinked_user_is_ignored(pg_session):
    client = _FakeClient()
    req = _interactive_req(
        "env-int6",
        FEEDBACK_UP_ACTION_ID,
        user="U0NOBODYINT2",
        value="pd-1::event::evt-1",
    )

    # must not raise
    handle_socket_mode_request(
        client, req, session_factory=_FakeSessionFactory(session=pg_session)
    )

    assert client.acks == ["env-int6"]


def test_confirm_log_button_click_schedules_the_job(
    monkeypatch, pg_session, ensure_test_user
):
    ensure_test_user("usr_int7", slack_user_id="U0INT7")
    scheduled = []
    monkeypatch.setattr(
        "app.triggers.slack.slack_socket_listener._schedule",
        lambda target, *args: scheduled.append((target, args)),
    )

    client = _FakeClient()
    req = _interactive_req(
        "env-int7",
        FRIDAY_REVIEW_CONFIRM_LOG_ACTION_ID,
        user="U0INT7",
        channel="D0INT7",
        value="delivery-abc-123",
    )

    handle_socket_mode_request(
        client, req, session_factory=_FakeSessionFactory(session=pg_session)
    )

    assert client.acks == ["env-int7"]
    assert len(scheduled) == 1
    target, args = scheduled[0]
    assert target.__name__ == "_run_confirm_log_with_own_session"
    assert args[1:] == ("usr_int7", "delivery-abc-123", "D0INT7", "500.1")


def test_confirm_log_click_from_an_unlinked_user_is_ignored(pg_session):
    client = _FakeClient()
    req = _interactive_req(
        "env-int8",
        FRIDAY_REVIEW_CONFIRM_LOG_ACTION_ID,
        user="U0NOBODYINT3",
        value="delivery-xyz",
    )

    # must not raise
    handle_socket_mode_request(
        client, req, session_factory=_FakeSessionFactory(session=pg_session)
    )

    assert client.acks == ["env-int8"]


def test_persist_slack_message_writes_a_message_row(
    monkeypatch, pg_session, ensure_test_user
):
    ensure_test_user("usr_persist1", slack_user_id="U0PERSIST1")

    class _DisabledDeliverer:
        enabled = False

    monkeypatch.setattr(
        "app.triggers.slack.slack_socket_listener.SlackDeliverer", _DisabledDeliverer
    )

    event = {
        "ts": "1700000000.000100",
        "user": "U0OTHER",
        "text": "<@U0PERSIST1> can you take a look?",
    }

    _persist_slack_message_with_own_session(
        lambda: pg_session, "usr_persist1", event, "C0SOMECHANNEL"
    )

    scope = OwnerScope(owner_user_id="usr_persist1", session=pg_session)
    rows = (
        pg_session.execute(
            scope.query(Message).where(Message.external_id == "1700000000.000100")
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].channel == "C0SOMECHANNEL"
    assert rows[0].url is None  # deliverer disabled -> no permalink lookup
    assert rows[0].body_ref == "slack://C0SOMECHANNEL/1700000000.000100"


def test_persist_slack_message_swallows_its_own_failures(monkeypatch):
    def _boom():
        raise RuntimeError("db unavailable")

    # must not raise even though the session factory itself blows up
    _persist_slack_message_with_own_session(
        _boom, "usr_whatever", {"ts": "1.1", "user": "U0X"}, "C0X"
    )


def _agenda_items_for(pg_session, report_user_id):
    from app.agenda.models import AgendaItem

    return (
        pg_session.execute(
            select(AgendaItem).where(AgendaItem.report_user_id == report_user_id)
        )
        .scalars()
        .all()
    )


def test_persist_slack_message_creates_shared_item_when_report_mentions_manager(
    monkeypatch, pg_session, make_pair
):
    # make_pair mints fresh random user ids each call — reused across test
    # runs against the same (no-per-test-rollback) DB would otherwise trip
    # uq_pair_one_active_per_report on a second run.
    pair = make_pair()

    class _DisabledDeliverer:
        enabled = False

    monkeypatch.setattr(
        "app.triggers.slack.slack_socket_listener.SlackDeliverer", _DisabledDeliverer
    )

    event = {
        "ts": "1700000000.000200",
        "user": "U0REPORTER",
        "text": "<@U0MANAGER> can you take a look?",
    }

    _persist_slack_message_with_own_session(
        lambda: pg_session,
        pair.report_user_id,
        event,
        "C0SOMECHANNEL",
        [
            (
                pair.report_user_id,
                _AgendaMentionContext(
                    manager_user_id=pair.manager_user_id,
                    sender_user_id=pair.report_user_id,
                    mentions_report=False,
                    mentions_manager=True,
                ),
            )
        ],
    )

    rows = _agenda_items_for(pg_session, pair.report_user_id)
    assert len(rows) == 1
    assert rows[0].source == "slack"
    assert rows[0].visibility == "shared"


def test_persist_slack_message_creates_shared_item_when_manager_mentions_report(
    monkeypatch, pg_session, make_pair
):
    pair = make_pair()

    class _DisabledDeliverer:
        enabled = False

    monkeypatch.setattr(
        "app.triggers.slack.slack_socket_listener.SlackDeliverer", _DisabledDeliverer
    )

    event = {
        "ts": "1700000000.000201",
        "user": "U0MANAGER",
        "text": "<@U0REPORT> what's the status on this?",
    }

    _persist_slack_message_with_own_session(
        lambda: pg_session,
        pair.report_user_id,
        event,
        "C0SOMECHANNEL",
        [
            (
                pair.report_user_id,
                _AgendaMentionContext(
                    manager_user_id=pair.manager_user_id,
                    sender_user_id=pair.manager_user_id,
                    mentions_report=True,
                    mentions_manager=False,
                ),
            )
        ],
    )

    rows = _agenda_items_for(pg_session, pair.report_user_id)
    assert len(rows) == 1
    assert rows[0].visibility == "shared"


def test_persist_slack_message_creates_report_only_item_from_a_third_party(
    monkeypatch, pg_session, make_pair
):
    """Someone who isn't the report or the manager mentioning the report is
    a private heads-up, not a confirmed shared topic — report_only, not
    shared."""
    pair = make_pair()

    class _DisabledDeliverer:
        enabled = False

    monkeypatch.setattr(
        "app.triggers.slack.slack_socket_listener.SlackDeliverer", _DisabledDeliverer
    )

    event = {
        "ts": "1700000000.000202",
        "user": "U0THIRDPARTY",
        "text": "<@U0REPORT> can you review my PR?",
    }

    _persist_slack_message_with_own_session(
        lambda: pg_session,
        pair.report_user_id,
        event,
        "C0SOMECHANNEL",
        [
            (
                pair.report_user_id,
                _AgendaMentionContext(
                    manager_user_id=pair.manager_user_id,
                    sender_user_id=None,  # unlinked third party
                    mentions_report=True,
                    mentions_manager=False,
                ),
            )
        ],
    )

    rows = _agenda_items_for(pg_session, pair.report_user_id)
    assert len(rows) == 1
    assert rows[0].visibility == "report_only"


def test_persist_slack_message_creates_manager_only_item_from_a_third_party(
    monkeypatch, pg_session, make_pair
):
    pair = make_pair()

    class _DisabledDeliverer:
        enabled = False

    monkeypatch.setattr(
        "app.triggers.slack.slack_socket_listener.SlackDeliverer", _DisabledDeliverer
    )

    event = {
        "ts": "1700000000.000203",
        "user": "U0THIRDPARTY",
        "text": "<@U0MANAGER> heads up, is the deploy delayed?",
    }

    _persist_slack_message_with_own_session(
        lambda: pg_session,
        pair.report_user_id,
        event,
        "C0SOMECHANNEL",
        [
            (
                pair.report_user_id,
                _AgendaMentionContext(
                    manager_user_id=pair.manager_user_id,
                    sender_user_id=None,
                    mentions_report=False,
                    mentions_manager=True,
                ),
            )
        ],
    )

    rows = _agenda_items_for(pg_session, pair.report_user_id)
    assert len(rows) == 1
    assert rows[0].visibility == "manager_only"


def test_persist_slack_message_does_not_flag_a_non_question(
    monkeypatch, pg_session, make_pair
):
    pair = make_pair()

    class _DisabledDeliverer:
        enabled = False

    monkeypatch.setattr(
        "app.triggers.slack.slack_socket_listener.SlackDeliverer", _DisabledDeliverer
    )

    event = {
        "ts": "1700000000.000300",
        "user": "U0THIRDPARTY",
        "text": "<@U0REPORT> nice work on the release",
    }

    _persist_slack_message_with_own_session(
        lambda: pg_session,
        pair.report_user_id,
        event,
        "C0SOMECHANNEL",
        _AgendaMentionContext(
            manager_user_id=pair.manager_user_id,
            sender_user_id=None,
            mentions_report=True,
            mentions_manager=False,
        ),
    )

    assert _agenda_items_for(pg_session, pair.report_user_id) == []


def test_persist_slack_message_does_not_flag_when_no_agenda_signal_given(
    monkeypatch, pg_session, make_pair
):
    """The DM path (owner talking to the bot) never passes an
    agenda_signal — a question-shaped DM must not create an agenda item
    even though _looks_like_open_question would match it."""
    pair = make_pair()

    class _DisabledDeliverer:
        enabled = False

    monkeypatch.setattr(
        "app.triggers.slack.slack_socket_listener.SlackDeliverer", _DisabledDeliverer
    )

    event = {
        "ts": "1700000000.000400",
        "user": pair.report_user_id,
        "text": "pulse?",
    }

    _persist_slack_message_with_own_session(
        lambda: pg_session, pair.report_user_id, event, "C0SOMECHANNEL"
    )

    assert _agenda_items_for(pg_session, pair.report_user_id) == []


def test_persist_slack_message_self_mention_is_not_a_signal(
    monkeypatch, pg_session, make_pair
):
    """The report mentioning the manager is only meaningful when the
    manager is who's mentioned (and vice versa) — sender == mentioned
    party (a self-mention/quirk) produces nothing."""
    pair = make_pair()

    class _DisabledDeliverer:
        enabled = False

    monkeypatch.setattr(
        "app.triggers.slack.slack_socket_listener.SlackDeliverer", _DisabledDeliverer
    )

    event = {
        "ts": "1700000000.000500",
        "user": "U0REPORTER",
        "text": "<@U0REPORT> reminding myself?",
    }

    _persist_slack_message_with_own_session(
        lambda: pg_session,
        pair.report_user_id,
        event,
        "C0SOMECHANNEL",
        _AgendaMentionContext(
            manager_user_id=pair.manager_user_id,
            sender_user_id=pair.report_user_id,
            mentions_report=True,
            mentions_manager=False,
        ),
    )

    assert _agenda_items_for(pg_session, pair.report_user_id) == []
