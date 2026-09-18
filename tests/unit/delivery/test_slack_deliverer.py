from typing import ClassVar

from slack_sdk.errors import SlackApiError

from app.delivery.slack_deliverer import SlackDeliverer, slack_enabled


class _FakeSlackResponse(dict):
    """slack_sdk raises SlackApiError(msg, response) where response supports
    response["error"] — a plain dict already satisfies that."""


class _FakeWebClient:
    calls: ClassVar[list[tuple[str, dict]]] = []
    reaction_error: ClassVar[str | None] = None

    def __init__(self, token):
        self.token = token

    def chat_postMessage(self, **kwargs):
        _FakeWebClient.calls.append(("chat_postMessage", kwargs))
        return {"ts": "123.456", "channel": kwargs.get("channel", "D_RESOLVED")}

    def chat_postEphemeral(self, **kwargs):
        _FakeWebClient.calls.append(("chat_postEphemeral", kwargs))
        return {"ok": True}

    def reactions_add(self, **kwargs):
        _FakeWebClient.calls.append(("reactions_add", kwargs))
        if _FakeWebClient.reaction_error:
            raise SlackApiError(
                "boom", _FakeSlackResponse(error=_FakeWebClient.reaction_error)
            )
        return {"ok": True}

    def files_upload_v2(self, **kwargs):
        _FakeWebClient.calls.append(("files_upload_v2", kwargs))
        return {"ts": "888.111"}


def test_slack_enabled_reflects_env(monkeypatch):
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    assert slack_enabled() is False

    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-fake")
    assert slack_enabled() is True


def test_deliverer_is_a_noop_without_a_token():
    deliverer = SlackDeliverer(token=None)

    assert deliverer.enabled is False
    result = deliverer.deliver("U123", blocks=[{"type": "section"}])

    assert result["sent"] is False
    assert "SLACK_BOT_TOKEN" in result["reason"]


def test_deliverer_never_calls_the_network_without_a_token(monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("should never construct a WebClient without a token")

    monkeypatch.setattr("app.delivery.slack_deliverer.WebClient", _boom)
    deliverer = SlackDeliverer(token=None)

    deliverer.deliver("U123", blocks=[{"type": "section"}])  # must not raise


def test_dm_only_invocation_posts_only_to_the_user(monkeypatch):
    _FakeWebClient.calls = []
    monkeypatch.setattr("app.delivery.slack_deliverer.WebClient", _FakeWebClient)
    deliverer = SlackDeliverer(token="xoxb-fake")

    result = deliverer.deliver("U123", blocks=[{"type": "section"}])

    assert result["sent"] is True
    assert result["posted_publicly"] is False
    kinds = [call for call, _kwargs in _FakeWebClient.calls]
    assert kinds == ["chat_postMessage"]


def test_deliver_returns_the_resolved_dm_channel_for_a_later_audio_upload(monkeypatch):
    # files.completeUploadExternal (upload_audio) rejects a bare user id —
    # it needs the DM's actual resolved channel id, which only
    # chat_postMessage's own response carries.
    _FakeWebClient.calls = []
    monkeypatch.setattr("app.delivery.slack_deliverer.WebClient", _FakeWebClient)
    deliverer = SlackDeliverer(token="xoxb-fake")

    result = deliverer.deliver("U123", blocks=[{"type": "section"}])

    assert result["dm_channel"] == "U123"  # fake echoes the channel it was given
    _, kwargs = _FakeWebClient.calls[0]
    assert kwargs["channel"] == "U123"
    # Slack requires a top-level text fallback for push notifications and
    # screen readers — omitting it produces a runtime warning even though
    # blocks alone still render fine in the client.
    assert kwargs["text"]


def test_channel_invocation_redirects_to_dm_with_ephemeral_ack(monkeypatch):
    _FakeWebClient.calls = []
    monkeypatch.setattr("app.delivery.slack_deliverer.WebClient", _FakeWebClient)
    deliverer = SlackDeliverer(token="xoxb-fake")

    result = deliverer.deliver("U123", blocks=[{"type": "section"}], channel_id="C999")

    assert result["sent"] is True
    assert result["posted_publicly"] is False
    kinds = [call for call, _kwargs in _FakeWebClient.calls]
    assert kinds == ["chat_postMessage", "chat_postEphemeral"]

    dm_kwargs = _FakeWebClient.calls[0][1]
    assert dm_kwargs["channel"] == "U123"  # the pulse itself only ever goes to DM

    ack_kwargs = _FakeWebClient.calls[1][1]
    assert ack_kwargs["channel"] == "C999"
    assert ack_kwargs["user"] == "U123"
    assert "DM" in ack_kwargs["text"]
    assert "thread_ts" not in ack_kwargs


def test_channel_invocation_with_thread_ts_posts_a_public_threaded_reply_not_ephemeral(
    monkeypatch,
):
    # Slack doesn't reliably render an ephemeral message as the first reply
    # in a thread, so a threaded ack must be a normal chat.postMessage, not
    # chat.postEphemeral.
    _FakeWebClient.calls = []
    monkeypatch.setattr("app.delivery.slack_deliverer.WebClient", _FakeWebClient)
    deliverer = SlackDeliverer(token="xoxb-fake")

    deliverer.deliver(
        "U123", blocks=[{"type": "section"}], channel_id="C999", thread_ts="555.111"
    )

    kinds = [call for call, _kwargs in _FakeWebClient.calls]
    assert kinds == ["chat_postMessage", "chat_postMessage"]
    ack_kwargs = _FakeWebClient.calls[1][1]
    assert ack_kwargs["thread_ts"] == "555.111"
    assert ack_kwargs["channel"] == "C999"
    assert "DM" in ack_kwargs["text"]


def test_post_channel_message_posts_publicly(monkeypatch):
    _FakeWebClient.calls = []
    monkeypatch.setattr("app.delivery.slack_deliverer.WebClient", _FakeWebClient)
    deliverer = SlackDeliverer(token="xoxb-fake")

    result = deliverer.post_channel_message("C999", "working on it")

    assert result == {"sent": True, "ts": "123.456"}
    assert _FakeWebClient.calls == [
        ("chat_postMessage", {"channel": "C999", "text": "working on it"})
    ]


def test_post_channel_message_is_a_noop_without_a_token(monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("should never construct a WebClient without a token")

    monkeypatch.setattr("app.delivery.slack_deliverer.WebClient", _boom)
    deliverer = SlackDeliverer(token=None)

    result = deliverer.post_channel_message("C999", "working on it")

    assert result["sent"] is False


def test_dm_thread_ts_threads_the_card_itself_under_a_prior_dm_ack(monkeypatch):
    _FakeWebClient.calls = []
    monkeypatch.setattr("app.delivery.slack_deliverer.WebClient", _FakeWebClient)
    deliverer = SlackDeliverer(token="xoxb-fake")

    result = deliverer.deliver(
        "U123", blocks=[{"type": "section"}], dm_thread_ts="777.222"
    )

    assert result["sent"] is True
    kinds = [call for call, _kwargs in _FakeWebClient.calls]
    assert kinds == ["chat_postMessage"]  # no channel_id -> no channel-side ack
    dm_kwargs = _FakeWebClient.calls[0][1]
    assert dm_kwargs["channel"] == "U123"
    assert dm_kwargs["thread_ts"] == "777.222"


def test_upload_audio_posts_a_threaded_file(monkeypatch):
    _FakeWebClient.calls = []
    monkeypatch.setattr("app.delivery.slack_deliverer.WebClient", _FakeWebClient)
    deliverer = SlackDeliverer(token="xoxb-fake")

    result = deliverer.upload_audio(
        "U123",
        audio_bytes=b"fake-mp3-bytes",
        filename="pulse.mp3",
        title="Your morning pulse",
        thread_ts="777.222",
    )

    assert result == {"sent": True, "ts": "888.111"}
    kinds = [call for call, _kwargs in _FakeWebClient.calls]
    assert kinds == ["files_upload_v2"]
    kwargs = _FakeWebClient.calls[0][1]
    assert kwargs["channel"] == "U123"
    assert kwargs["content"] == b"fake-mp3-bytes"
    assert kwargs["filename"] == "pulse.mp3"
    assert kwargs["thread_ts"] == "777.222"


def test_upload_audio_is_a_noop_without_a_token(monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("should never construct a WebClient without a token")

    monkeypatch.setattr("app.delivery.slack_deliverer.WebClient", _boom)
    deliverer = SlackDeliverer(token=None)

    result = deliverer.upload_audio(
        "U123", audio_bytes=b"x", filename="pulse.mp3", title="Your morning pulse"
    )

    assert result["sent"] is False


def test_without_dm_thread_ts_the_card_is_a_fresh_top_level_dm_message(monkeypatch):
    _FakeWebClient.calls = []
    monkeypatch.setattr("app.delivery.slack_deliverer.WebClient", _FakeWebClient)
    deliverer = SlackDeliverer(token="xoxb-fake")

    deliverer.deliver("U123", blocks=[{"type": "section"}])

    dm_kwargs = _FakeWebClient.calls[0][1]
    assert "thread_ts" not in dm_kwargs


def test_post_thread_reply_threads_under_the_given_ts(monkeypatch):
    _FakeWebClient.calls = []
    monkeypatch.setattr("app.delivery.slack_deliverer.WebClient", _FakeWebClient)
    deliverer = SlackDeliverer(token="xoxb-fake")

    result = deliverer.post_thread_reply(
        "D123", blocks=[{"type": "section"}], text="Your full shortlist", thread_ts="1.1"
    )

    assert result == {"sent": True, "ts": "123.456", "channel": "D123"}
    assert _FakeWebClient.calls == [
        (
            "chat_postMessage",
            {
                "channel": "D123",
                "blocks": [{"type": "section"}],
                "text": "Your full shortlist",
                "thread_ts": "1.1",
            },
        )
    ]


def test_post_thread_reply_without_thread_ts_posts_a_fresh_message(monkeypatch):
    _FakeWebClient.calls = []
    monkeypatch.setattr("app.delivery.slack_deliverer.WebClient", _FakeWebClient)
    deliverer = SlackDeliverer(token="xoxb-fake")

    deliverer.post_thread_reply("D123", blocks=[{"type": "section"}], text="hi")

    assert "thread_ts" not in _FakeWebClient.calls[0][1]


def test_post_thread_reply_is_a_noop_without_a_token(monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("should never construct a WebClient without a token")

    monkeypatch.setattr("app.delivery.slack_deliverer.WebClient", _boom)
    deliverer = SlackDeliverer(token=None)

    result = deliverer.post_thread_reply("D123", blocks=[], text="hi")

    assert result["sent"] is False


def test_add_reaction_calls_reactions_add(monkeypatch):
    _FakeWebClient.calls = []
    _FakeWebClient.reaction_error = None
    monkeypatch.setattr("app.delivery.slack_deliverer.WebClient", _FakeWebClient)
    deliverer = SlackDeliverer(token="xoxb-fake")

    deliverer.add_reaction("D123", "123.456", "eyes")

    assert _FakeWebClient.calls == [
        ("reactions_add", {"channel": "D123", "timestamp": "123.456", "name": "eyes"})
    ]


def test_add_reaction_swallows_already_reacted(monkeypatch):
    _FakeWebClient.calls = []
    _FakeWebClient.reaction_error = "already_reacted"
    monkeypatch.setattr("app.delivery.slack_deliverer.WebClient", _FakeWebClient)
    deliverer = SlackDeliverer(token="xoxb-fake")

    # must not raise — the reaction already being there is not a failure
    deliverer.add_reaction("D123", "123.456", "eyes")

    _FakeWebClient.reaction_error = None


def test_add_reaction_reraises_other_errors(monkeypatch):
    _FakeWebClient.calls = []
    _FakeWebClient.reaction_error = "invalid_auth"
    monkeypatch.setattr("app.delivery.slack_deliverer.WebClient", _FakeWebClient)
    deliverer = SlackDeliverer(token="xoxb-fake")

    try:
        deliverer.add_reaction("D123", "123.456", "eyes")
        raised = False
    except SlackApiError:
        raised = True
    finally:
        _FakeWebClient.reaction_error = None

    assert raised is True


def test_add_reaction_is_a_noop_without_a_token(monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("should never construct a WebClient without a token")

    monkeypatch.setattr("app.delivery.slack_deliverer.WebClient", _boom)
    deliverer = SlackDeliverer(token=None)

    deliverer.add_reaction("D123", "123.456", "eyes")  # must not raise


def test_send_text_message_posts_plain_text(monkeypatch):
    _FakeWebClient.calls = []
    monkeypatch.setattr("app.delivery.slack_deliverer.WebClient", _FakeWebClient)
    deliverer = SlackDeliverer(token="xoxb-fake")

    deliverer.send_text_message("D123", "not linked")

    assert _FakeWebClient.calls == [
        ("chat_postMessage", {"channel": "D123", "text": "not linked"})
    ]


def test_send_text_message_is_a_noop_without_a_token(monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("should never construct a WebClient without a token")

    monkeypatch.setattr("app.delivery.slack_deliverer.WebClient", _boom)
    deliverer = SlackDeliverer(token=None)

    deliverer.send_text_message("D123", "not linked")  # must not raise
