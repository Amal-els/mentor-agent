import datetime

from app.delivery.tts import build_friday_review_speech_script, deliver_friday_review_audio
from app.sub_agents.friday_review.sub_agents.synthesize.agent import FridayReviewCard, ScoredWin


def _base_card(**overrides) -> FridayReviewCard:
    defaults = dict(
        week_start=datetime.date(2026, 8, 24), wins=[], slipped_lines=[],
        one_adjustment=None, agenda_resolved_lines=[], agenda_stuck_lines=[],
        okr_progress_lines=[], career_narrative=None, daily_pulse_patterns=[],
        skill_distribution_summary=None, quiet_week=False, identity_asks_count=0,
    )
    defaults.update(overrides)
    return FridayReviewCard(**defaults)


def test_quiet_week_script_says_so():
    script = build_friday_review_speech_script(_base_card(quiet_week=True))
    assert "quiet" in script.lower()


def test_script_mentions_each_win():
    card = _base_card(
        wins=[
            ScoredWin(text="Shipped the migration", source_link=None, source_reference_key="w1",
                      moved_goal_title=None, already_logged=False, skill_category=None)
        ]
    )
    script = build_friday_review_speech_script(card)
    assert "Shipped the migration" in script


class _FakeDeliverer:
    enabled = True

    def __init__(self):
        self.uploaded = []

    def upload_audio(self, channel_id, audio_bytes, filename, title, thread_ts):
        self.uploaded.append((channel_id, filename, thread_ts))


def test_deliver_never_raises_when_tts_unavailable(monkeypatch):
    monkeypatch.setattr("app.delivery.tts.synthesize_speech", lambda text: None)
    deliverer = _FakeDeliverer()
    deliver_friday_review_audio(deliverer, _base_card(), "D1", "123.456")
    assert deliverer.uploaded == []  # no audio -> no upload, no crash


def test_deliver_uploads_when_tts_available(monkeypatch):
    monkeypatch.setattr(
        "app.delivery.tts.draft_friday_review_audio_script", lambda card: None
    )
    monkeypatch.setattr("app.delivery.tts.synthesize_speech", lambda text: b"fake-mp3-bytes")
    deliverer = _FakeDeliverer()
    deliver_friday_review_audio(deliverer, _base_card(), "D1", "123.456")
    assert len(deliverer.uploaded) == 1
    assert deliverer.uploaded[0] == ("D1", "friday_review.mp3", "123.456")


def test_deliver_disabled_deliverer_never_calls_tts(monkeypatch):
    called = {}

    def _fail(text):
        called["hit"] = True
        return b"x"

    monkeypatch.setattr("app.delivery.tts.synthesize_speech", _fail)

    class _DisabledDeliverer:
        enabled = False

    deliver_friday_review_audio(_DisabledDeliverer(), _base_card(), "D1", "123.456")
    assert "hit" not in called
