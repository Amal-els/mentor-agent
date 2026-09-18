from app.delivery.cards import PulseCard
from app.delivery.tts import (
    build_pulse_speech_script,
    deliver_pulse_audio,
    draft_audio_script,
    narrator_agent,
    synthesize_speech,
)
from app.sub_agents.pulse.sub_agents.writer.agent import PulseItem


def _card(focus, suggested_focus=None):
    return PulseCard(
        focus=focus,
        day=[],
        owed=[],
        suggested_focus=suggested_focus,
        degradation_line=None,
        since_note=None,
        shown_count=len(focus),
        total_count=len(focus),
        footer="prompt_ids=[] context_hash=x",
    )


def test_empty_focus_says_nothing_cleared_the_bar():
    script = build_pulse_speech_script(_card([]))

    assert script == "Good morning. Nothing cleared the bar today."


def test_script_never_contains_markdown_or_urls():
    card = _card(
        [
            PulseItem(
                item_id="evt-1",
                title="1:1 with Sarah",
                why_now="Sarah is waiting on your reply.",
                action="Reply before noon.",
                url="https://example.com/evt-1",
            )
        ],
        suggested_focus="Reply before noon.",
    )

    script = build_pulse_speech_script(card)

    assert "https://" not in script
    assert "*" not in script
    assert "_" not in script
    assert "<" not in script


def test_script_mentions_each_focus_item_by_title():
    card = _card(
        [
            PulseItem(item_id="a", title="Ship the release notes", why_now="Overdue", action="Send now"),
            PulseItem(item_id="b", title="Review PR #42", why_now="Marc is waiting", action="Approve or comment"),
        ]
    )

    script = build_pulse_speech_script(card)

    assert "Ship the release notes" in script
    assert "Review PR #42" in script
    assert "2 things" in script


def test_script_uses_singular_phrasing_for_one_item():
    card = _card(
        [PulseItem(item_id="a", title="Only thing", why_now="It's due", action="Do it")]
    )

    script = build_pulse_speech_script(card)

    assert "one thing" in script


def test_script_ends_with_the_suggested_focus_when_present():
    card = _card(
        [PulseItem(item_id="a", title="Thing", why_now="Reason", action="Step")],
        suggested_focus="Step",
    )

    script = build_pulse_speech_script(card)

    assert script.rstrip().endswith("make it: Step")


def test_script_omits_the_suggested_focus_line_when_absent():
    card = _card(
        [PulseItem(item_id="a", title="Thing", why_now="Reason", action="Step")],
        suggested_focus=None,
    )

    script = build_pulse_speech_script(card)

    assert "make it" not in script


def test_synthesize_speech_returns_none_without_crashing_when_tts_call_fails(
    monkeypatch,
):
    import app.delivery.tts as tts_module

    class _BoomClient:
        def __init__(self, *a, **k):
            raise RuntimeError("no credentials")

    monkeypatch.setattr(
        "google.cloud.texttospeech.TextToSpeechClient", _BoomClient
    )

    assert synthesize_speech("hello") is None


def test_draft_audio_script_returns_the_llm_script_on_success(monkeypatch):
    def fake_run_agent_sync(agent, initial_state, **kwargs):
        assert agent is narrator_agent
        return {"narrator_result": {"script": "A warm natural summary."}}

    monkeypatch.setattr("app.delivery.tts.run_agent_sync", fake_run_agent_sync)

    card = _card(
        [PulseItem(item_id="a", title="Thing", why_now="Reason", action="Step")]
    )

    assert draft_audio_script(card) == "A warm natural summary."


def test_draft_audio_script_returns_none_without_crashing_on_failure(monkeypatch):
    def _boom(agent, initial_state, **kwargs):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr("app.delivery.tts.run_agent_sync", _boom)

    card = _card(
        [PulseItem(item_id="a", title="Thing", why_now="Reason", action="Step")]
    )

    assert draft_audio_script(card) is None


class _FakeDeliverer:
    def __init__(self, enabled=True):
        self.enabled = enabled
        self.uploaded = None

    def upload_audio(self, channel_id, audio_bytes, filename, title, thread_ts=None):
        self.uploaded = (channel_id, audio_bytes, filename, title, thread_ts)
        return {"sent": True}


def test_deliver_pulse_audio_uses_the_llm_script_when_available(monkeypatch):
    monkeypatch.setattr("app.delivery.tts.draft_audio_script", lambda card: "LLM script")
    monkeypatch.setattr("app.delivery.tts.synthesize_speech", lambda text: text.encode())

    card = _card([PulseItem(item_id="a", title="Thing", why_now="R", action="S")])
    deliverer = _FakeDeliverer()

    deliver_pulse_audio(deliverer, card, "D123", "999.111")

    assert deliverer.uploaded[1] == b"LLM script"


def test_deliver_pulse_audio_falls_back_to_the_template_when_llm_draft_fails(
    monkeypatch,
):
    monkeypatch.setattr("app.delivery.tts.draft_audio_script", lambda card: None)
    monkeypatch.setattr("app.delivery.tts.synthesize_speech", lambda text: text.encode())

    card = _card([PulseItem(item_id="a", title="Thing", why_now="R", action="S")])
    deliverer = _FakeDeliverer()

    deliver_pulse_audio(deliverer, card, "D123", "999.111")

    assert deliverer.uploaded[1] == build_pulse_speech_script(card).encode()
