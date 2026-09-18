import datetime

from app.delivery.cards import FRIDAY_REVIEW_CONFIRM_LOG_ACTION_ID, build_friday_review_card
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


def test_quiet_week_renders_a_plain_statement_and_skips_adjustment():
    card = _base_card(quiet_week=True)
    blocks = build_friday_review_card(card, delivery_id="d1", proposed_ledger_items=[])
    text_blob = " ".join(
        b["text"]["text"] for b in blocks if b.get("type") == "section" and "text" in b
    )
    assert "quiet week" in text_blob.lower()
    assert "adjustment" not in text_blob.lower()


def test_confirm_log_button_only_rendered_when_items_proposed():
    card = _base_card(
        wins=[
            ScoredWin(text="Shipped X", source_link="https://x/1", source_reference_key="wi-1",
                      moved_goal_title=None, already_logged=False, skill_category=None)
        ]
    )
    blocks_with_items = build_friday_review_card(
        card, delivery_id="d1",
        proposed_ledger_items=[{"description": "Shipped X", "source_reference_key": "wi-1"}],
    )
    action_ids = [
        el.get("action_id")
        for b in blocks_with_items if b.get("type") == "actions"
        for el in b.get("elements", [])
    ]
    assert FRIDAY_REVIEW_CONFIRM_LOG_ACTION_ID in action_ids

    blocks_without_items = build_friday_review_card(card, delivery_id="d1", proposed_ledger_items=[])
    action_ids_without = [
        el.get("action_id")
        for b in blocks_without_items if b.get("type") == "actions"
        for el in b.get("elements", [])
    ]
    assert FRIDAY_REVIEW_CONFIRM_LOG_ACTION_ID not in action_ids_without


def test_empty_skill_distribution_renders_not_enough_history():
    card = _base_card(skill_distribution_summary=None)
    blocks = build_friday_review_card(card, delivery_id="d1", proposed_ledger_items=[])
    text_blob = " ".join(
        b["text"]["text"] for b in blocks if b.get("type") == "section" and "text" in b
    )
    assert "not enough categorized history yet" in text_blob.lower()


def test_identity_asks_section_omitted_when_zero():
    card = _base_card(identity_asks_count=0)
    blocks = build_friday_review_card(card, delivery_id="d1", proposed_ledger_items=[])
    text_blob = " ".join(
        b["text"]["text"] for b in blocks if b.get("type") == "section" and "text" in b
    )
    assert "who is this" not in text_blob.lower()


def test_stuck_agenda_items_rendered():
    card = _base_card(agenda_stuck_lines=["Renegotiate the deadline with Sam"])
    blocks = build_friday_review_card(card, delivery_id="d1", proposed_ledger_items=[])
    text_blob = " ".join(
        b["text"]["text"] for b in blocks if b.get("type") == "section" and "text" in b
    )
    assert "Renegotiate the deadline with Sam" in text_blob


def test_next_week_focus_rendered_when_present():
    """REAL CHANGE (requested: the Friday reflection didn't say what
    needs to be worked on next week)."""
    card = _base_card(next_week_focus_lines=["Push the migration OKR past 50%"])
    blocks = build_friday_review_card(card, delivery_id="d1", proposed_ledger_items=[])
    text_blob = " ".join(
        b["text"]["text"] for b in blocks if b.get("type") == "section" and "text" in b
    )
    assert "Focus for next week" in text_blob
    assert "Push the migration OKR past 50%" in text_blob


def test_next_week_focus_omitted_when_empty():
    card = _base_card(next_week_focus_lines=[])
    blocks = build_friday_review_card(card, delivery_id="d1", proposed_ledger_items=[])
    text_blob = " ".join(
        b["text"]["text"] for b in blocks if b.get("type") == "section" and "text" in b
    )
    assert "Focus for next week" not in text_blob
