import datetime
import uuid

from app.agenda.models import AgendaItem
from app.agenda.scope import resolve_pair_scope
from app.agenda.store import append_agenda_item, get_agenda
from app.core.clock import FrozenClock
from app.delivery.cards import build_agenda_summary_blocks
from app.sub_agents.agenda.agent import send_post_meeting_slack_dm
from app.triggers.agenda.agenda_signal_handler import handle_agenda_signal

NOW = datetime.datetime(2026, 8, 16, 10, 0, 0, tzinfo=datetime.UTC)


def test_realtime_agenda_payload_assembly(db_session, make_pair, make_user):
    pair = make_pair()
    scope = resolve_pair_scope(db_session, pair.report_user_id, pair.report_user_id)
    clock = FrozenClock(at=NOW)

    item1 = append_agenda_item(
        scope,
        {
            "text": "Review architecture doc",
            "source": "manual",
            "visibility": "shared",
            "created_by_user_id": pair.report_user_id,
            "created_by_role": "report",
        },
        clock,
    )

    item2 = append_agenda_item(
        scope,
        {
            "text": "Fix flaky test",
            "source": "jira",
            "source_link": "https://jira.internal/browse/BUG-1",
            "visibility": "shared",
            "created_by_user_id": pair.manager_user_id,
            "created_by_role": "manager",
        },
        clock,
    )

    agenda = get_agenda(scope)
    assert len(agenda) == 2

    # Verify Slack block formatting for real-time summary delivery
    items = [
        {"text": item1.text, "source": item1.source, "visibility": item1.visibility},
        {"text": item2.text, "source": item2.source, "visibility": item2.visibility},
    ]

    blocks = build_agenda_summary_blocks(items)
    assert len(blocks) >= 4
    # blocks[0] = header, blocks[1] = divider, blocks[2] = section header, blocks[3] = item 1, blocks[4] = item 2
    assert "Review architecture doc" in blocks[3]["text"]["text"]
    assert "Fix flaky test" in blocks[4]["text"]["text"]


def test_ag_ui_signal_handler_keep_and_drop_flow(db_session, make_pair):
    pair = make_pair()

    item = AgendaItem(
        id=str(uuid.uuid4()),
        report_user_id=pair.report_user_id,
        pair_id=pair.id,
        text="Stale discussion topic",
        source="meeting_synthesis",
        source_link=None,
        visibility="shared",
        status="pending_consent",
        surfaced_count=5,
        created_at=NOW,
        created_by_user_id=pair.manager_user_id,
        created_by_role="manager",
    )
    db_session.add(item)
    db_session.commit()

    # Test "keep" signal: resets count to 0, status to "open"
    keep_signal = {
        "signal_type": "keep",
        "report_user_id": pair.report_user_id,
        "acting_user_id": pair.report_user_id,
        "item_id": item.id,
    }
    result_keep = handle_agenda_signal(db_session, keep_signal)
    assert result_keep["status"] == "ok"

    updated = db_session.get(AgendaItem, item.id)
    assert updated.status == "open"
    assert updated.surfaced_count == 0

    # Flip to pending_consent and test "drop" signal: sets status to "resolved"
    updated.status = "pending_consent"
    db_session.commit()

    drop_signal = {
        "signal_type": "drop",
        "report_user_id": pair.report_user_id,
        "acting_user_id": pair.report_user_id,
        "item_id": item.id,
    }
    result_drop = handle_agenda_signal(db_session, drop_signal)
    assert result_drop["status"] == "ok"

    dropped = db_session.get(AgendaItem, item.id)
    assert dropped.status == "resolved"
    assert dropped.resolved_at is not None


def test_slack_dm_delivery_fallback_when_unconfigured(db_session, make_pair):
    pair = make_pair()
    scope = resolve_pair_scope(db_session, pair.report_user_id, pair.report_user_id)

    res = send_post_meeting_slack_dm(scope)
    # SLACK_BOT_TOKEN is not set in test environment -> returns clean sent=False fallback
    assert res["sent"] is False
    assert res["reason"] == "SLACK_BOT_TOKEN not configured"


def test_slack_dm_delivery_respects_visibility_per_recipient(
    db_session, make_pair, monkeypatch
):
    """Reproduces the composition-review finding: report_only/manager_only
    items must never reach the other party's DM."""
    pair = make_pair()
    report_scope = resolve_pair_scope(
        db_session, pair.report_user_id, pair.report_user_id
    )
    manager_scope = resolve_pair_scope(
        db_session, pair.report_user_id, pair.manager_user_id
    )
    clock = FrozenClock(at=NOW)

    append_agenda_item(
        report_scope,
        {
            "text": "private report note",
            "source": "manual",
            "visibility": "report_only",
            "created_by_user_id": pair.report_user_id,
            "created_by_role": "report",
        },
        clock,
    )
    append_agenda_item(
        manager_scope,
        {
            "text": "private manager note",
            "source": "manual",
            "visibility": "manager_only",
            "created_by_user_id": pair.manager_user_id,
            "created_by_role": "manager",
        },
        clock,
    )

    from app.core.models import User

    report_user = db_session.get(User, pair.report_user_id)
    manager_user = db_session.get(User, pair.manager_user_id)
    report_slack_id = f"U_REPORT_{uuid.uuid4().hex[:8]}"
    manager_slack_id = f"U_MANAGER_{uuid.uuid4().hex[:8]}"
    report_user.slack_user_id = report_slack_id
    manager_user.slack_user_id = manager_slack_id
    db_session.commit()

    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    sent_blocks = {}

    class _FakeDeliverer:
        enabled = True

        def deliver(self, slack_user_id, blocks, **kwargs):
            sent_blocks[slack_user_id] = blocks
            return {"ok": True}

    monkeypatch.setattr(
        "app.delivery.slack_deliverer.SlackDeliverer", lambda: _FakeDeliverer()
    )

    send_post_meeting_slack_dm(report_scope)

    report_text = str(sent_blocks[report_slack_id])
    manager_text = str(sent_blocks[manager_slack_id])
    assert "private report note" in report_text
    assert "private report note" not in manager_text
    assert "private manager note" in manager_text
    assert "private manager note" not in report_text


def _fake_deliverer_setup(monkeypatch, db_session, pair):
    """Shared setup for the only_item_ids tests below: real SLACK_BOT_TOKEN
    env + a fake deliverer that records exactly what was sent, matching
    test_slack_dm_delivery_respects_visibility_per_recipient's own
    pattern above."""
    import uuid

    from app.core.models import User

    report_user = db_session.get(User, pair.report_user_id)
    slack_id = f"U_REPORT_{uuid.uuid4().hex[:8]}"
    report_user.slack_user_id = slack_id
    db_session.commit()

    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    sent_blocks = {}

    class _FakeDeliverer:
        enabled = True

        def deliver(self, slack_user_id, blocks, **kwargs):
            sent_blocks[slack_user_id] = blocks
            return {"ok": True}

    monkeypatch.setattr(
        "app.delivery.slack_deliverer.SlackDeliverer", lambda: _FakeDeliverer()
    )
    return slack_id, sent_blocks


def test_slack_dm_only_item_ids_restricts_to_just_those_items(
    db_session, make_pair, monkeypatch
):
    """REAL CHANGE (requested): the post-meeting DM should read as a
    short "what came out of this meeting" summary, not the full agenda —
    only_item_ids is what makes that possible."""
    pair = make_pair()
    report_scope = resolve_pair_scope(
        db_session, pair.report_user_id, pair.report_user_id
    )
    clock = FrozenClock(at=NOW)

    old_item = append_agenda_item(
        report_scope,
        {
            "text": "an old item from a previous meeting",
            "source": "manual",
            "visibility": "shared",
            "created_by_user_id": pair.report_user_id,
            "created_by_role": "report",
        },
        clock,
    )
    new_item = append_agenda_item(
        report_scope,
        {
            "text": "brand new from this meeting",
            "source": "meeting_synthesis",
            "visibility": "shared",
            "created_by_user_id": pair.report_user_id,
            "created_by_role": "report",
        },
        clock,
    )

    slack_id, sent_blocks = _fake_deliverer_setup(monkeypatch, db_session, pair)

    send_post_meeting_slack_dm(report_scope, only_item_ids={new_item.id})

    text = str(sent_blocks[slack_id])
    assert "brand new from this meeting" in text
    assert "an old item from a previous meeting" not in text


def test_slack_dm_skips_sending_when_only_item_ids_is_empty(
    db_session, make_pair, monkeypatch
):
    """A meeting that produced nothing new (only decisions/focus_points,
    no ledger item or fresh agenda entry) must not send a DM at all —
    see send_post_meeting_slack_dm's own docstring on why this is the
    expected common case, not a failure."""
    pair = make_pair()
    report_scope = resolve_pair_scope(
        db_session, pair.report_user_id, pair.report_user_id
    )
    _slack_id, sent_blocks = _fake_deliverer_setup(monkeypatch, db_session, pair)

    result = send_post_meeting_slack_dm(report_scope, only_item_ids=set())

    assert result == {"sent": False, "reason": "nothing new to summarize"}
    assert sent_blocks == {}
