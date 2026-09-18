from app.delivery.tts import (
    build_dossier_speech_script,
    deliver_dossier_audio,
    dossier_narrator_agent,
    draft_dossier_audio_script,
)
from app.sub_agents.dossier.sub_agents.synthesize.agent import DossierCard, TalkingPoint


def _card(**overrides):
    defaults = {
        "who": ["Sam External"],
        "why_now": "Renewal is next week.",
        "talking_points": [
            TalkingPoint(text="Discuss renewal", source_link="https://x/1")
        ],
        "promised_and_not_delivered": [],
        "blockers": [],
        "suggested_opener": "Ask how the migration went.",
        "short_version": False,
        "nothing_to_prep": False,
    }
    defaults.update(overrides)
    return DossierCard(**defaults)


def test_nothing_to_prep_says_so_and_stops_there():
    script = build_dossier_speech_script(_card(nothing_to_prep=True, talking_points=[]))
    assert script == "Nothing to prep for this one — no real signal to brief you on."


def test_script_opens_with_event_title_when_given():
    card = _card(who=["Jordan Prospect"])
    script = build_dossier_speech_script(card, event_title="Weekly 1:1 with Jordan")
    assert script.startswith("Let's get prepared for Weekly 1:1 with Jordan")
    assert "Jordan Prospect" in script


def test_script_falls_back_to_who_only_opener_without_event_title():
    card = _card(who=["Jordan Prospect"])
    script = build_dossier_speech_script(card)
    assert script.startswith("Let's get prepared. You're meeting with Jordan Prospect")


def test_script_never_contains_markdown_or_urls():
    script = build_dossier_speech_script(_card())
    assert "https://" not in script
    assert "*" not in script
    assert "_" not in script
    assert "<" not in script


def test_script_mentions_who_and_each_talking_point():
    card = _card(
        who=["Jordan Prospect"],
        talking_points=[
            TalkingPoint(text="Confirm the renewal date", source_link="https://x/1"),
            TalkingPoint(
                text="Ask about the integration blocker", source_link="https://x/2"
            ),
        ],
    )
    script = build_dossier_speech_script(card)
    assert "Jordan Prospect" in script
    assert "Confirm the renewal date" in script
    assert "Ask about the integration blocker" in script
    assert "2 things" in script


def test_script_uses_singular_phrasing_for_one_talking_point():
    card = _card(
        talking_points=[TalkingPoint(text="Only thing", source_link="https://x/1")]
    )
    script = build_dossier_speech_script(card)
    assert "one thing" in script


def test_script_ends_with_the_suggested_opener_when_present():
    card = _card(suggested_opener="Ask how the migration went.")
    script = build_dossier_speech_script(card)
    assert script.rstrip().endswith("Ask how the migration went.")


def test_script_omits_opener_line_when_absent():
    card = _card(suggested_opener=None)
    script = build_dossier_speech_script(card)
    assert "To open:" not in script


def test_script_mentions_promised_and_not_delivered():
    card = _card(promised_and_not_delivered=["Alex owes the roadmap doc"])
    script = build_dossier_speech_script(card)
    assert "Alex owes the roadmap doc" in script


def test_draft_dossier_audio_script_returns_the_llm_script_on_success(monkeypatch):
    def fake_run_agent_sync(agent, initial_state, **kwargs):
        assert agent is dossier_narrator_agent
        return {"dossier_narrator_result": {"script": "Vibrant advice narration."}}

    monkeypatch.setattr("app.delivery.tts.run_agent_sync", fake_run_agent_sync)

    assert draft_dossier_audio_script(_card()) == "Vibrant advice narration."


def test_draft_dossier_audio_script_returns_none_without_crashing_on_failure(
    monkeypatch,
):
    def _boom(agent, initial_state, **kwargs):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr("app.delivery.tts.run_agent_sync", _boom)

    assert draft_dossier_audio_script(_card()) is None


class _FakeDeliverer:
    def __init__(self, enabled=True):
        self.enabled = enabled
        self.uploaded = None

    def upload_audio(self, channel_id, audio_bytes, filename, title, thread_ts=None):
        self.uploaded = (channel_id, audio_bytes, filename, title, thread_ts)
        return {"sent": True}


def test_deliver_dossier_audio_uses_the_llm_script_when_available(monkeypatch):
    monkeypatch.setattr(
        "app.delivery.tts.draft_dossier_audio_script",
        lambda card, event_title=None: "LLM script",
    )
    monkeypatch.setattr(
        "app.delivery.tts.synthesize_speech", lambda text: text.encode()
    )

    deliverer = _FakeDeliverer()
    deliver_dossier_audio(deliverer, _card(), "D123", "999.111")

    assert deliverer.uploaded[1] == b"LLM script"
    assert deliverer.uploaded[4] == "999.111"


def test_deliver_dossier_audio_falls_back_to_the_template_when_llm_draft_fails(
    monkeypatch,
):
    monkeypatch.setattr(
        "app.delivery.tts.draft_dossier_audio_script",
        lambda card, event_title=None: None,
    )
    monkeypatch.setattr(
        "app.delivery.tts.synthesize_speech", lambda text: text.encode()
    )

    card = _card()
    deliverer = _FakeDeliverer()
    deliver_dossier_audio(deliverer, card, "D123", "999.111")

    assert deliverer.uploaded[1] == build_dossier_speech_script(card).encode()


def test_deliver_dossier_audio_is_a_noop_when_deliverer_disabled(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "app.delivery.tts.draft_dossier_audio_script",
        lambda card: calls.append(True),
    )
    deliverer = _FakeDeliverer(enabled=False)

    deliver_dossier_audio(deliverer, _card(), "D123", "999.111")

    assert calls == []
    assert deliverer.uploaded is None


def test_deliver_dossier_audio_skips_upload_when_synthesis_fails(monkeypatch):
    monkeypatch.setattr(
        "app.delivery.tts.draft_dossier_audio_script",
        lambda card, event_title=None: "script",
    )
    monkeypatch.setattr("app.delivery.tts.synthesize_speech", lambda text: None)
    deliverer = _FakeDeliverer()

    deliver_dossier_audio(deliverer, _card(), "D123", "999.111")

    assert deliverer.uploaded is None
