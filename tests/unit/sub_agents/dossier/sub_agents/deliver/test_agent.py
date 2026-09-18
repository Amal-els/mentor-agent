import datetime
import uuid

from app.core.clock import FrozenClock
from app.core.models import DossierDelivery, User
from app.core.scope import OwnerScope
from app.sub_agents.dossier.sub_agents.deliver.agent import deliver_dossier
from app.sub_agents.dossier.sub_agents.synthesize.agent import DossierCard, TalkingPoint

NOW = datetime.datetime(2026, 8, 19, 9, 0, 0, tzinfo=datetime.UTC)


class _FakeDeliverer:
    # Matches SlackDeliverer.deliver()'s REAL return shape (dm_ts/
    # dm_channel, not "ts") -- the old version of this fake used an
    # invented {"ts": ...} shape that never matched the real deliverer,
    # which is exactly how deliver_dossier's card_ref bug (reading
    # result.get("ts") against a dict that only ever has "dm_ts") went
    # uncaught through every prior review of this feature.
    enabled = True

    def __init__(self):
        self.sent = []
        self.uploaded = []

    def deliver(
        self, user_slack_id, blocks, channel_id=None, thread_ts=None, dm_thread_ts=None
    ):
        self.sent.append((user_slack_id, blocks))
        return {"sent": True, "dm_ts": "123.456", "dm_channel": "D0FAKE"}

    def upload_audio(self, channel_id, audio_bytes, filename, title, thread_ts=None):
        self.uploaded.append((channel_id, audio_bytes, filename, title, thread_ts))
        return {"sent": True}

    def get_permalink(self, channel_id, message_ts):
        return f"https://example.slack.com/archives/{channel_id}/p{message_ts}"


def test_deliver_writes_dossier_delivery_row(pg_session, monkeypatch):
    monkeypatch.setattr(
        "app.sub_agents.dossier.sub_agents.deliver.agent.deliver_dossier_audio",
        lambda *a, **k: None,
    )
    slack_user_id = f"U{uuid.uuid4().hex[:8]}"
    user = User(id=str(uuid.uuid4()), created_at=NOW, slack_user_id=slack_user_id)
    pg_session.add(user)
    pg_session.commit()
    scope = OwnerScope(owner_user_id=user.id, session=pg_session)

    card = DossierCard(
        who=["Sam External"],
        why_now="Renewal is next week.",
        talking_points=[
            TalkingPoint(text="Discuss renewal", source_link="https://x/1")
        ],
        promised_and_not_delivered=[],
        blockers=[],
        suggested_opener="Ask how the migration went.",
        short_version=False,
        nothing_to_prep=False,
    )
    deliverer = _FakeDeliverer()
    clock = FrozenClock(at=NOW)

    delivery = deliver_dossier(
        card,
        event_external_id=f"evt-{uuid.uuid4()}",
        owner_scope=scope,
        slack_user_id=slack_user_id,
        prompt_version="dossier_synthesize@v1",
        talking_points_source="fresh",
        deliverer=deliverer,
        clock=clock,
        event_title="Weekly 1:1 with Sam",
        event_starts_at="2026-08-19T14:00:00+00:00",
    )

    assert len(deliverer.sent) == 1
    fetched = pg_session.get(DossierDelivery, delivery.id)
    # Proves deliver_dossier now uses the injected Clock, not wall-clock
    # SystemClock() (finding 8 of the whole-branch review fix wave).
    assert fetched.sent_at == NOW
    assert fetched.created_at == NOW
    assert fetched.prompt_version == "dossier_synthesize@v1"
    assert fetched.who_summary == "Sam External"
    assert fetched.why_now == "Renewal is next week."
    # card_ref must come from the real deliverer's "dm_ts" key, not the
    # nonexistent "ts" key the old code read (always None against the
    # real SlackDeliverer -- see _FakeDeliverer's own docstring above).
    assert fetched.card_ref == "123.456"
    assert fetched.dm_channel == "D0FAKE"

    _, sent_blocks = deliverer.sent[0]
    header_text = sent_blocks[0]["text"]["text"]
    assert "Weekly 1:1 with Sam" in header_text


def test_deliver_dossier_does_not_resend_for_an_existing_delivery(
    pg_session, monkeypatch
):
    monkeypatch.setattr(
        "app.sub_agents.dossier.sub_agents.deliver.agent.deliver_dossier_audio",
        lambda *a, **k: None,
    )
    # finding 4: a second deliver_dossier call for the same (owner, event)
    # must not send a second real Slack DM or crash on the
    # UNIQUE(owner_user_id, event_external_id) constraint — it must return
    # the existing row instead.
    slack_user_id = f"U{uuid.uuid4().hex[:8]}"
    user = User(id=str(uuid.uuid4()), created_at=NOW, slack_user_id=slack_user_id)
    pg_session.add(user)
    pg_session.commit()
    scope = OwnerScope(owner_user_id=user.id, session=pg_session)
    event_external_id = f"evt-{uuid.uuid4()}"

    card = DossierCard(
        who=["Sam External"],
        why_now="Renewal is next week.",
        talking_points=[
            TalkingPoint(text="Discuss renewal", source_link="https://x/1")
        ],
        promised_and_not_delivered=[],
        blockers=[],
        suggested_opener="Ask how the migration went.",
        short_version=False,
        nothing_to_prep=False,
    )
    deliverer = _FakeDeliverer()
    clock = FrozenClock(at=NOW)

    first = deliver_dossier(
        card,
        event_external_id=event_external_id,
        owner_scope=scope,
        slack_user_id=slack_user_id,
        prompt_version="dossier_synthesize@v1",
        talking_points_source="fresh",
        deliverer=deliverer,
        clock=clock,
    )
    second = deliver_dossier(
        card,
        event_external_id=event_external_id,
        owner_scope=scope,
        slack_user_id=slack_user_id,
        prompt_version="dossier_synthesize@v1",
        talking_points_source="fresh",
        deliverer=deliverer,
        clock=clock,
    )

    # A repeat prep for an already-delivered event must not go silent — a
    # normal top-level message (not buried in a thread) with a permalink
    # back to the original card, so the requester lands back on what they
    # already have without having to scroll and expand anything.
    assert len(deliverer.sent) == 2
    assert second.id == first.id
    _, notice_blocks = deliverer.sent[1]
    notice_text = notice_blocks[0]["text"]["text"]
    assert "Nothing new since your last prep for this meeting." in notice_text
    assert "Jump to it" in notice_text
    assert "D0FAKE/p123.456" in notice_text


def test_deliver_dossier_skips_notice_when_original_never_sent(
    pg_session, monkeypatch
):
    monkeypatch.setattr(
        "app.sub_agents.dossier.sub_agents.deliver.agent.deliver_dossier_audio",
        lambda *a, **k: None,
    )
    slack_user_id = f"U{uuid.uuid4().hex[:8]}"
    user = User(id=str(uuid.uuid4()), created_at=NOW, slack_user_id=slack_user_id)
    pg_session.add(user)
    pg_session.commit()
    scope = OwnerScope(owner_user_id=user.id, session=pg_session)
    event_external_id = f"evt-{uuid.uuid4()}"

    card = DossierCard(
        who=["Sam External"],
        why_now="Renewal is next week.",
        talking_points=[],
        promised_and_not_delivered=[],
        blockers=[],
        suggested_opener=None,
        short_version=False,
        nothing_to_prep=True,
    )

    class _FailingDeliverer:
        enabled = True

        def __init__(self):
            self.sent = []

        def deliver(self, *a, **k):
            self.sent.append((a, k))
            return {"sent": False}

        def get_permalink(self, *a, **k):
            raise AssertionError(
                "get_permalink must not be called when there's no card_ref to link to"
            )

    deliverer = _FailingDeliverer()
    clock = FrozenClock(at=NOW)

    deliver_dossier(
        card,
        event_external_id=event_external_id,
        owner_scope=scope,
        slack_user_id=slack_user_id,
        prompt_version="dossier_synthesize@v1",
        talking_points_source="fresh",
        deliverer=deliverer,
        clock=clock,
    )
    deliver_dossier(
        card,
        event_external_id=event_external_id,
        owner_scope=scope,
        slack_user_id=slack_user_id,
        prompt_version="dossier_synthesize@v1",
        talking_points_source="fresh",
        deliverer=deliverer,
        clock=clock,
    )

    # card_ref is None because the original send never actually succeeded
    # (sent=False) — no message exists to link back to, so the second call
    # must not attempt a "nothing new" notice at all (only the one real
    # attempt from the first call).
    assert len(deliverer.sent) == 1


def test_deliver_dossier_triggers_the_audio_companion_on_success(
    pg_session, monkeypatch
):
    calls = []
    monkeypatch.setattr(
        "app.sub_agents.dossier.sub_agents.deliver.agent.deliver_dossier_audio",
        lambda deliverer, card, channel_id, thread_ts, event_title=None: calls.append(
            (channel_id, thread_ts)
        ),
    )
    slack_user_id = f"U{uuid.uuid4().hex[:8]}"
    user = User(id=str(uuid.uuid4()), created_at=NOW, slack_user_id=slack_user_id)
    pg_session.add(user)
    pg_session.commit()
    scope = OwnerScope(owner_user_id=user.id, session=pg_session)

    card = DossierCard(
        who=["Sam External"],
        why_now="Renewal is next week.",
        talking_points=[
            TalkingPoint(text="Discuss renewal", source_link="https://x/1")
        ],
        promised_and_not_delivered=[],
        blockers=[],
        suggested_opener="Ask how the migration went.",
        short_version=False,
        nothing_to_prep=False,
    )
    deliverer = _FakeDeliverer()

    deliver_dossier(
        card,
        event_external_id=f"evt-{uuid.uuid4()}",
        owner_scope=scope,
        slack_user_id=slack_user_id,
        prompt_version="dossier_synthesize@v1",
        talking_points_source="fresh",
        deliverer=deliverer,
        clock=FrozenClock(at=NOW),
    )

    # Threaded under the exact message that was just sent -- the fake
    # deliverer's own dm_channel/dm_ts, not the caller's slack_user_id.
    assert calls == [("D0FAKE", "123.456")]


def test_deliver_dossier_skips_the_audio_companion_when_send_fails(
    pg_session, monkeypatch
):
    calls = []
    monkeypatch.setattr(
        "app.sub_agents.dossier.sub_agents.deliver.agent.deliver_dossier_audio",
        lambda *a, **k: calls.append(True),
    )

    class _FailingDeliverer:
        enabled = True

        def deliver(self, *a, **k):
            return {"sent": False}

    slack_user_id = f"U{uuid.uuid4().hex[:8]}"
    user = User(id=str(uuid.uuid4()), created_at=NOW, slack_user_id=slack_user_id)
    pg_session.add(user)
    pg_session.commit()
    scope = OwnerScope(owner_user_id=user.id, session=pg_session)

    card = DossierCard(
        who=["Sam External"],
        why_now="Renewal is next week.",
        talking_points=[],
        promised_and_not_delivered=[],
        blockers=[],
        suggested_opener=None,
        short_version=False,
        nothing_to_prep=True,
    )

    deliver_dossier(
        card,
        event_external_id=f"evt-{uuid.uuid4()}",
        owner_scope=scope,
        slack_user_id=slack_user_id,
        prompt_version="dossier_synthesize@v1",
        talking_points_source="fresh",
        deliverer=_FailingDeliverer(),
        clock=FrozenClock(at=NOW),
    )

    assert calls == []
