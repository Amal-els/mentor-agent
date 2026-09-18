import datetime

from app.core.clock import FrozenClock
from app.core.models import Event, Message, WorkItem
from app.salience.pulse.score import (
    is_resolved_work_item,
    score_event,
    score_message,
    score_work_item,
)

NOW = FrozenClock(at=datetime.datetime(2026, 8, 7, 7, 0, tzinfo=datetime.UTC))


def _event(**overrides) -> Event:
    defaults = {
        "id": "evt-1",
        "owner_user_id": "usr_amal",
        "actor_reference_key": "calendar:sarah@acme.com",
        "resolved_person_id": "person-sarah",
        "source": "calendar",
        "external_id": "evt-1",
        "title": "1:1 with Sarah",
        "starts_at": datetime.datetime(2026, 8, 7, 9, 0, tzinfo=datetime.UTC),
        "ends_at": datetime.datetime(2026, 8, 7, 9, 30, tzinfo=datetime.UTC),
        "status": "confirmed",
        "series_id": None,
    }
    defaults.update(overrides)
    return Event(**defaults)


def _work_item(**overrides) -> WorkItem:
    defaults = {
        "id": "wi-1",
        "owner_user_id": "usr_amal",
        "actor_reference_key": "linear:sarah@acme.com",
        "resolved_person_id": "person-sarah",
        "source": "linear",
        "external_id": "MENT-214",
        "title": "Fix flaky roster test",
        "status": "blocked",
        "due_at": datetime.datetime(2026, 8, 7, 18, 0, tzinfo=datetime.UTC),
        "updated_at": datetime.datetime(2026, 8, 7, 6, 0, tzinfo=datetime.UTC),
        "blocks_others": False,
    }
    defaults.update(overrides)
    return WorkItem(**defaults)


def _message(**overrides) -> Message:
    defaults = {
        "id": "msg-1",
        "owner_user_id": "usr_amal",
        "actor_reference_key": "slack:U123",
        "resolved_person_id": "person-sarah",
        "source": "slack",
        "external_id": "1699999999.0001",
        "channel": "C0123",
        "sent_at": datetime.datetime(2026, 8, 7, 9, 0, tzinfo=datetime.UTC),
        "is_dm": False,
        "body_ref": "slack://C0123/1699999999.0001",
    }
    defaults.update(overrides)
    return Message(**defaults)


def test_event_today_with_resolved_person_is_candidate_focus():
    scored = score_event(
        _event(), person_name="Sarah Ben Youssef", weight=1.0, clock=NOW
    )

    assert scored.item_type == "event"
    assert scored.score_terms["due_today"] is True
    assert scored.score_terms["person_waiting"] == "Sarah Ben Youssef"
    assert scored.candidate_focus is True


def test_event_without_resolved_person_is_not_candidate_focus():
    scored = score_event(_event(), person_name=None, weight=1.0, clock=NOW)

    assert scored.score_terms["person_waiting"] is None
    assert scored.candidate_focus is False


def test_event_not_today_has_no_urgency():
    future = _event(
        starts_at=datetime.datetime(2026, 8, 10, 9, 0, tzinfo=datetime.UTC),
        ends_at=datetime.datetime(2026, 8, 10, 9, 30, tzinfo=datetime.UTC),
    )
    scored = score_event(future, person_name="Sarah Ben Youssef", weight=1.0, clock=NOW)

    assert scored.score_terms["due_today"] is False
    assert scored.candidate_focus is False


def test_event_score_scales_with_weight():
    scored_neutral = score_event(_event(), person_name=None, weight=1.0, clock=NOW)
    scored_boosted = score_event(_event(), person_name=None, weight=2.0, clock=NOW)

    assert scored_boosted.score == scored_neutral.score * 2.0


def test_blocked_work_item_due_today_with_resolved_person_is_candidate_focus():
    scored = score_work_item(
        _work_item(), person_name="Sarah Ben Youssef", weight=1.0, clock=NOW
    )

    assert scored.item_type == "work_item"
    assert scored.score_terms["blocked"] is True
    assert scored.score_terms["due_today"] is True
    assert scored.candidate_focus is True


def test_work_item_overdue_scores_higher_than_due_today():
    overdue = _work_item(
        due_at=datetime.datetime(2026, 8, 6, 18, 0, tzinfo=datetime.UTC)
    )
    due_today = _work_item()

    scored_overdue = score_work_item(overdue, person_name=None, weight=1.0, clock=NOW)
    scored_due_today = score_work_item(
        due_today, person_name=None, weight=1.0, clock=NOW
    )

    assert scored_overdue.score > scored_due_today.score
    assert scored_overdue.score_terms["overdue"] is True


def test_work_item_not_blocked_is_not_candidate_focus_even_with_person():
    in_progress = _work_item(status="in_progress")
    scored = score_work_item(
        in_progress, person_name="Sarah Ben Youssef", weight=1.0, clock=NOW
    )

    assert scored.score_terms["person_waiting"] is None
    assert scored.candidate_focus is False


def test_work_item_far_future_due_date_has_no_urgency():
    far_future = _work_item(
        due_at=datetime.datetime(2026, 8, 20, 18, 0, tzinfo=datetime.UTC)
    )
    scored = score_work_item(
        far_future, person_name="Sarah Ben Youssef", weight=1.0, clock=NOW
    )

    assert scored.score_terms["has_deadline"] is False
    assert scored.candidate_focus is False


def test_work_item_no_due_date_has_no_urgency():
    no_due = _work_item(due_at=None)
    scored = score_work_item(
        no_due, person_name="Sarah Ben Youssef", weight=1.0, clock=NOW
    )

    assert scored.score_terms["has_deadline"] is False


def test_message_sent_today_with_resolved_person_is_candidate_focus():
    scored = score_message(
        _message(), person_name="Sarah Ben Youssef", weight=1.0, clock=NOW
    )

    assert scored.item_type == "message"
    assert scored.score_terms["due_today"] is True
    assert scored.score_terms["person_waiting"] == "Sarah Ben Youssef"
    assert scored.candidate_focus is True


def test_message_without_resolved_person_is_not_candidate_focus():
    scored = score_message(_message(), person_name=None, weight=1.0, clock=NOW)

    assert scored.score_terms["person_waiting"] is None
    assert scored.candidate_focus is False


def test_message_not_sent_today_has_no_urgency():
    old = _message(sent_at=datetime.datetime(2026, 8, 1, 9, 0, tzinfo=datetime.UTC))
    scored = score_message(old, person_name="Sarah Ben Youssef", weight=1.0, clock=NOW)

    assert scored.score_terms["due_today"] is False
    assert scored.candidate_focus is False


def test_message_score_scales_with_weight():
    scored_neutral = score_message(_message(), person_name=None, weight=1.0, clock=NOW)
    scored_boosted = score_message(_message(), person_name=None, weight=2.0, clock=NOW)

    assert scored_boosted.score == scored_neutral.score * 2.0


# --- Priority-ranking signals (command-center-brief priority table) --------


def test_work_item_blocking_others_scores_higher_and_flags_the_term():
    blocker = _work_item(blocks_others=True)
    non_blocker = _work_item(blocks_others=False)

    scored_blocker = score_work_item(blocker, person_name=None, weight=1.0, clock=NOW)
    scored_non_blocker = score_work_item(
        non_blocker, person_name=None, weight=1.0, clock=NOW
    )

    assert scored_blocker.score_terms["blocks_others"] is True
    assert scored_blocker.score > scored_non_blocker.score


def test_work_item_stale_after_threshold_scores_higher_and_scales_with_age():
    fresh = _work_item(
        updated_at=datetime.datetime(2026, 8, 7, 6, 0, tzinfo=datetime.UTC)
    )
    stale = _work_item(
        updated_at=datetime.datetime(2026, 8, 3, 6, 0, tzinfo=datetime.UTC)
    )
    very_stale = _work_item(
        updated_at=datetime.datetime(2026, 7, 20, 6, 0, tzinfo=datetime.UTC)
    )

    scored_fresh = score_work_item(fresh, person_name=None, weight=1.0, clock=NOW)
    scored_stale = score_work_item(stale, person_name=None, weight=1.0, clock=NOW)
    scored_very_stale = score_work_item(
        very_stale, person_name=None, weight=1.0, clock=NOW
    )

    assert scored_fresh.score_terms["stale"] is False
    assert scored_stale.score_terms["stale"] is True
    assert scored_stale.score < scored_very_stale.score
    # capped — an ancient item doesn't score unboundedly higher forever
    assert scored_very_stale.score_terms["days_stale"] > 10


def test_work_item_no_updated_at_has_no_staleness():
    no_updated = _work_item(updated_at=None)
    scored = score_work_item(no_updated, person_name=None, weight=1.0, clock=NOW)

    assert scored.score_terms["stale"] is False
    assert scored.score_terms["days_stale"] == 0


def test_work_item_meeting_today_scores_higher_and_flags_the_term():
    scored_with_meeting = score_work_item(
        _work_item(), person_name=None, weight=1.0, clock=NOW, meeting_today=True
    )
    scored_without_meeting = score_work_item(
        _work_item(), person_name=None, weight=1.0, clock=NOW, meeting_today=False
    )

    assert scored_with_meeting.score_terms["meeting_today"] is True
    assert scored_with_meeting.score > scored_without_meeting.score


def test_work_item_is_new_since_last_pulse_is_informational_only():
    """Source recency is explicitly "filters noise, not urgency itself" per
    the priority table — it's carried in score_terms but never a bonus."""
    scored_new = score_work_item(
        _work_item(),
        person_name=None,
        weight=1.0,
        clock=NOW,
        is_new_since_last_pulse=True,
    )
    scored_old = score_work_item(
        _work_item(),
        person_name=None,
        weight=1.0,
        clock=NOW,
        is_new_since_last_pulse=False,
    )

    assert scored_new.score_terms["is_new_since_last_pulse"] is True
    assert scored_old.score_terms["is_new_since_last_pulse"] is False
    assert scored_new.score == scored_old.score


def test_message_stale_after_threshold_scores_higher():
    # Both a day removed from due_today (sent_at != NOW's date), so
    # due_today's own urgency bonus doesn't confound the comparison —
    # isolates the staleness bonus specifically.
    not_stale = _message(sent_at=datetime.datetime(2026, 8, 6, 6, 0, tzinfo=datetime.UTC))
    stale = _message(sent_at=datetime.datetime(2026, 8, 3, 6, 0, tzinfo=datetime.UTC))

    scored_not_stale = score_message(not_stale, person_name=None, weight=1.0, clock=NOW)
    scored_stale = score_message(stale, person_name=None, weight=1.0, clock=NOW)

    assert scored_not_stale.score_terms["stale"] is False
    assert scored_stale.score_terms["stale"] is True
    assert scored_stale.score > scored_not_stale.score


def test_message_meeting_today_scores_higher_and_flags_the_term():
    scored_with_meeting = score_message(
        _message(), person_name=None, weight=1.0, clock=NOW, meeting_today=True
    )
    scored_without_meeting = score_message(
        _message(), person_name=None, weight=1.0, clock=NOW, meeting_today=False
    )

    assert scored_with_meeting.score_terms["meeting_today"] is True
    assert scored_with_meeting.score > scored_without_meeting.score


def test_message_action_requested_scores_higher_and_flags_the_term():
    scored_action = score_message(
        _message(action_requested=True), person_name=None, weight=1.0, clock=NOW
    )
    scored_no_action = score_message(
        _message(action_requested=False), person_name=None, weight=1.0, clock=NOW
    )

    assert scored_action.score_terms["action_requested"] is True
    assert scored_action.score > scored_no_action.score


def test_message_requires_reply_false_suppresses_person_waiting_even_when_resolved():
    # A resolved sender normally always sets person_waiting (Slack/Docs
    # behavior) — Gmail's triageInbox is the only source that can say "this
    # one doesn't actually need a reply" and have that respected.
    scored = score_message(
        _message(requires_reply=False),
        person_name="Sarah Ben Youssef",
        weight=1.0,
        clock=NOW,
    )

    assert scored.score_terms["person_waiting"] is None
    assert scored.candidate_focus is False


def test_message_requires_reply_unset_preserves_existing_person_waiting_behavior():
    # requires_reply is never set by Slack/Docs — None must behave exactly
    # like today: any resolved sender counts as person_waiting.
    scored = score_message(
        _message(), person_name="Sarah Ben Youssef", weight=1.0, clock=NOW
    )

    assert scored.score_terms["person_waiting"] == "Sarah Ben Youssef"


def test_message_requires_reply_true_keeps_person_waiting():
    scored = score_message(
        _message(requires_reply=True),
        person_name="Sarah Ben Youssef",
        weight=1.0,
        clock=NOW,
    )

    assert scored.score_terms["person_waiting"] == "Sarah Ben Youssef"


def test_is_resolved_work_item_true_for_merged_pr():
    assert is_resolved_work_item(_work_item(source="github", status="merged")) is True


def test_is_resolved_work_item_true_for_closed_github_issue():
    assert is_resolved_work_item(_work_item(source="github", status="closed")) is True


def test_is_resolved_work_item_false_for_open_pr():
    assert is_resolved_work_item(_work_item(source="github", status="open")) is False


def test_is_resolved_work_item_false_for_draft_pr():
    assert is_resolved_work_item(_work_item(source="github", status="draft")) is False


def test_is_resolved_work_item_matches_common_linear_jira_done_shapes():
    for status in ("Done", "DONE", "Cancelled", "Canceled", "Resolved", "Closed"):
        assert is_resolved_work_item(_work_item(source="jira", status=status)) is True


def test_is_resolved_work_item_false_for_todo_or_blocked():
    assert is_resolved_work_item(_work_item(source="jira", status="To Do")) is False
    assert is_resolved_work_item(_work_item(source="linear", status="blocked")) is False


def test_is_resolved_work_item_false_for_missing_status():
    assert is_resolved_work_item(_work_item(status=None)) is False
