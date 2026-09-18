import datetime

from app.core.clock import FrozenClock
from app.core.scope import OwnerScope
from app.delivery.cards import (
    FEEDBACK_DOWN_ACTION_ID,
    FEEDBACK_UP_ACTION_ID,
    SHOW_FULL_SHORTLIST_ACTION_ID,
    PulseCard,
    _personalization_note,
    build_card,
    card_to_dict,
    parse_feedback_value,
    render_blocks,
    render_shortlist_blocks,
    render_text,
)
from app.salience.pulse.types import ScoredItem
from app.ingest.fixture_source import (
    FixtureCalendarClient,
    FixtureLinearClient,
    FixtureSlackClient,
    load_day_fixture,
)
from app.ingest.seed import seed_fixture
from app.triggers.dispatch import handle_trigger
from app.triggers.pulse_trigger import build_pull_trigger


def _clients(fixture_name):
    fixture = load_day_fixture(fixture_name)
    return [
        FixtureCalendarClient(fixture),
        FixtureSlackClient(fixture),
        FixtureLinearClient(fixture),
    ]


def _clock_for(fixture_name):
    fixture = load_day_fixture(fixture_name)
    return FrozenClock(at=datetime.datetime.fromisoformat(fixture["now"]))


def _trigger_result(pg_session, fixture_name):
    counts = seed_fixture(pg_session, fixture_name)
    scope = OwnerScope(owner_user_id=counts["owner_user_id"], session=pg_session)
    clock = _clock_for(fixture_name)
    event = build_pull_trigger(scope.owner_user_id, scope.owner_user_id, clock.now())
    return handle_trigger(scope, clock, _clients(fixture_name), event)


def test_card_section_order_is_fixed(pg_session):
    result = _trigger_result(pg_session, "normal_day")

    card = build_card(result)
    text = render_text(card)

    focus_pos = text.index("Focus")
    day_pos = text.index("Today's meeting load")
    owed_pos = text.index("Owed")
    suggested_pos = text.index("Suggested focus")
    assert focus_pos < day_pos < owed_pos < suggested_pos


def test_card_to_dict_focus_items_carry_writer_fields_and_novelty(pg_session):
    result = _trigger_result(pg_session, "normal_day")
    card = build_card(result)

    data = card_to_dict(card)

    assert set(data.keys()) == {
        "focus", "day", "owed", "suggested_focus", "degradation_line",
        "since_note", "shown_count", "total_count", "also_happening_counts",
        "personalization_note",
    }
    assert len(data["focus"]) == len(card.focus)
    for raw, item in zip(data["focus"], card.focus):
        assert raw["item_id"] == item.item_id
        assert raw["title"] == item.title
        assert raw["why_now"] == item.why_now
        assert raw["action"] == item.action
        assert raw["url"] == item.url
        assert raw["detail"] == item.detail
        assert raw["novelty"] == card.focus_item_novelty.get(item.item_id)


def test_card_to_dict_day_and_owed_are_json_safe(pg_session):
    result = _trigger_result(pg_session, "normal_day")
    card = build_card(result)

    data = card_to_dict(card)

    assert len(data["day"]) == len(card.day)
    for raw, event in zip(data["day"], card.day):
        assert raw["title"] == event.title
        assert raw["starts_at"] == event.starts_at.isoformat()
        assert raw["has_dossier"] == event.has_dossier
    assert len(data["owed"]) == len(card.owed)
    for raw, owed in zip(data["owed"], card.owed):
        assert raw["description"] == owed.description
        assert raw["promised_to"] == owed.promised_to
        assert raw["overdue"] == owed.overdue


def test_card_to_dict_scalar_fields_match_card(pg_session):
    result = _trigger_result(pg_session, "normal_day")
    card = build_card(result)

    data = card_to_dict(card)

    assert data["suggested_focus"] == card.suggested_focus
    assert data["degradation_line"] == card.degradation_line
    assert data["since_note"] == card.since_note
    assert data["shown_count"] == card.shown_count
    assert data["total_count"] == card.total_count
    assert data["also_happening_counts"] == card.also_happening_counts
    assert data["personalization_note"] == card.personalization_note


def test_card_to_dict_is_json_serializable(pg_session):
    import json

    result = _trigger_result(pg_session, "normal_day")
    card = build_card(result)

    # round-trips cleanly — this is exactly what PulseDelivery.card_json
    # (JSONB) and the pulse_ready SSE event both do with it.
    json.dumps(card_to_dict(card))


def test_degradation_line_appears_before_focus(pg_session):
    result = _trigger_result(pg_session, "degraded_source")

    card = build_card(result)
    text = render_text(card)

    assert card.degradation_line is not None
    assert text.index(card.degradation_line) < text.index("Focus")


def test_no_degradation_line_when_healthy(pg_session):
    result = _trigger_result(pg_session, "normal_day")

    card = build_card(result)

    assert card.degradation_line is None


def test_footer_has_prompt_ids_and_context_hash(pg_session):
    result = _trigger_result(pg_session, "normal_day")

    card = build_card(result)

    assert "pulse_ranker.v1" in card.footer
    assert "pulse_writer.v1" in card.footer
    assert "pulse_critic.v1" in card.footer
    assert "context_hash=" in card.footer


def test_ask_for_the_rest_line_when_cut(pg_session):
    result = _trigger_result(pg_session, "normal_day")

    card = build_card(result)

    # normal_day: 6 shortlist candidates (incl. Sarah's DM, now that
    # Message rows are scored too), cut to 3 shown
    assert card.total_count == 6
    assert card.shown_count == 3
    text = render_text(card)
    assert "3 of 6" in text
    assert "ask for the rest" in text


def test_no_ask_for_the_rest_line_when_not_cut(pg_session):
    result = _trigger_result(pg_session, "clear_day")

    card = build_card(result)

    assert card.total_count == card.shown_count
    text = render_text(card)
    assert "ask for the rest" not in text


def test_suggested_focus_is_drawn_from_focus_not_invented(pg_session):
    result = _trigger_result(pg_session, "normal_day")

    card = build_card(result)

    assert card.suggested_focus is not None
    assert card.suggested_focus == card.focus[0].action


def test_day_items_carry_a_dossier_flag(pg_session):
    result = _trigger_result(pg_session, "normal_day")

    card = build_card(result)

    assert len(card.day) == 3
    assert all(hasattr(item, "has_dossier") for item in card.day)


def test_since_note_absent_on_first_delivery(pg_session):
    result = _trigger_result(pg_session, "normal_day")

    card = build_card(result)

    assert card.since_note is None


def test_since_note_present_on_second_pull_same_day(pg_session):
    counts = seed_fixture(pg_session, "normal_day")
    scope = OwnerScope(owner_user_id=counts["owner_user_id"], session=pg_session)
    clock = _clock_for("normal_day")
    event = build_pull_trigger(scope.owner_user_id, scope.owner_user_id, clock.now())

    handle_trigger(scope, clock, _clients("normal_day"), event)
    second = handle_trigger(scope, clock, _clients("normal_day"), event)

    card = build_card(second)

    assert card.since_note is not None
    assert card.since_note.startswith("since ")


def test_render_blocks_includes_the_show_full_shortlist_button_when_cut(pg_session):
    result = _trigger_result(pg_session, "normal_day")
    card = build_card(result)

    blocks = render_blocks(card)
    actions_blocks = [
        b
        for b in blocks
        if b.get("type") == "actions" and b.get("block_id") == "pulse_shortlist_actions"
    ]

    assert len(actions_blocks) == 1
    button = actions_blocks[0]["elements"][0]
    assert button["action_id"] == SHOW_FULL_SHORTLIST_ACTION_ID
    assert "6" in button["text"]["text"]


def test_render_blocks_omits_the_button_when_not_cut(pg_session):
    result = _trigger_result(pg_session, "clear_day")
    card = build_card(result)

    blocks = render_blocks(card)

    assert not [
        b
        for b in blocks
        if b.get("type") == "actions" and b.get("block_id") == "pulse_shortlist_actions"
    ]


def _scored_item(item_id, score, **score_terms):
    return ScoredItem(
        item_id=item_id,
        item_type="event",
        score=score,
        score_terms=score_terms,
        candidate_focus=True,
    )


def test_render_shortlist_blocks_lists_every_item_sorted_by_score():
    shortlist = [
        _scored_item("evt-1", 0.4, due_today=True),
        _scored_item("evt-2", 0.9, overdue=True, person_waiting="Sarah"),
    ]
    titles = {"evt-1": "Team standup", "evt-2": "Fix flaky test"}

    blocks = render_shortlist_blocks(shortlist, titles)

    section_texts = [b["text"]["text"] for b in blocks if b["type"] == "section"]
    # highest score first
    assert "Fix flaky test" in section_texts[1]
    assert "Sarah waiting" in section_texts[1]
    assert "Team standup" in section_texts[2]


def test_render_shortlist_blocks_links_the_title_when_a_url_is_given():
    blocks = render_shortlist_blocks(
        [_scored_item("evt-1", 0.5)],
        titles={"evt-1": "Team standup"},
        urls={"evt-1": "https://example.com/evt-1"},
    )

    section_texts = [b["text"]["text"] for b in blocks if b["type"] == "section"]
    assert "<https://example.com/evt-1|Team standup>" in section_texts[1]


def test_render_shortlist_blocks_plain_title_when_no_url():
    blocks = render_shortlist_blocks(
        [_scored_item("evt-1", 0.5)], titles={"evt-1": "Team standup"}
    )

    section_texts = [b["text"]["text"] for b in blocks if b["type"] == "section"]
    assert section_texts[1].startswith("*Team standup*")
    assert "<" not in section_texts[1]


def test_render_shortlist_blocks_falls_back_to_item_id_when_no_title():
    blocks = render_shortlist_blocks([_scored_item("evt-9", 0.5)], titles={})

    section_texts = [b["text"]["text"] for b in blocks if b["type"] == "section"]
    assert "evt-9" in section_texts[1]


def test_render_shortlist_blocks_handles_an_empty_shortlist():
    blocks = render_shortlist_blocks([], titles={})

    assert "nothing in today's shortlist" in blocks[-1]["text"]["text"]


def _bare_card(focus, delivery_id=None, focus_item_types=None):
    return PulseCard(
        focus=focus,
        day=[],
        owed=[],
        suggested_focus=None,
        degradation_line=None,
        since_note=None,
        shown_count=len(focus),
        total_count=len(focus),
        footer="prompt_ids=[] context_hash=x",
        delivery_id=delivery_id,
        focus_item_types=focus_item_types or {},
    )


def test_render_blocks_links_a_focus_item_title_when_it_has_a_url():
    from app.sub_agents.pulse.sub_agents.writer.agent import PulseItem

    card = _bare_card(
        [
            PulseItem(
                item_id="evt-1",
                title="1:1 with Sarah",
                why_now="Overdue",
                action="Deliver today",
                url="https://example.com/evt-1",
            )
        ]
    )

    blocks = render_blocks(card)

    focus_texts = [
        b["text"]["text"]
        for b in blocks
        if b["type"] == "section" and "1:1 with Sarah" in b["text"]["text"]
    ]
    assert focus_texts == [
        "1️⃣ *<https://example.com/evt-1|1:1 with Sarah>*\nOverdue\n_Deliver today_"
    ]


def test_render_blocks_plain_focus_title_when_no_url():
    from app.sub_agents.pulse.sub_agents.writer.agent import PulseItem

    card = _bare_card(
        [PulseItem(item_id="evt-1", title="1:1 with Sarah", why_now="Overdue", action="Go")]
    )

    blocks = render_blocks(card)

    focus_texts = [
        b["text"]["text"]
        for b in blocks
        if b["type"] == "section" and "1:1 with Sarah" in b["text"]["text"]
    ]
    assert focus_texts[0].startswith("1️⃣ *1:1 with Sarah*")
    assert "<http" not in focus_texts[0]


def test_render_blocks_includes_the_detail_line_when_present():
    from app.sub_agents.pulse.sub_agents.writer.agent import PulseItem

    card = _bare_card(
        [
            PulseItem(
                item_id="wi-1",
                title="Fix flaky roster test",
                why_now="Overdue",
                action="Fix it",
                detail="MENT-214 · Sarah Ben Youssef",
            )
        ]
    )

    blocks = render_blocks(card)

    focus_texts = [
        b["text"]["text"]
        for b in blocks
        if b["type"] == "section" and "Fix flaky roster test" in b["text"]["text"]
    ]
    assert focus_texts == [
        "1️⃣ *Fix flaky roster test*\n_MENT-214 · Sarah Ben Youssef_\nOverdue\n_Fix it_"
    ]


def test_render_blocks_omits_the_detail_line_when_absent():
    from app.sub_agents.pulse.sub_agents.writer.agent import PulseItem

    card = _bare_card(
        [PulseItem(item_id="evt-1", title="1:1 with Sarah", why_now="Overdue", action="Go")]
    )

    blocks = render_blocks(card)

    focus_texts = [
        b["text"]["text"]
        for b in blocks
        if b["type"] == "section" and "1:1 with Sarah" in b["text"]["text"]
    ]
    assert focus_texts == ["1️⃣ *1:1 with Sarah*\nOverdue\n_Go_"]


def test_render_text_includes_the_detail_line_when_present():
    from app.sub_agents.pulse.sub_agents.writer.agent import PulseItem

    card = _bare_card(
        [
            PulseItem(
                item_id="wi-1",
                title="Fix flaky roster test",
                why_now="Overdue",
                action="Fix it",
                detail="MENT-214 · Sarah Ben Youssef",
            )
        ]
    )

    text = render_text(card)

    assert "MENT-214 · Sarah Ben Youssef" in text


def test_render_text_includes_the_url_in_parens_when_present():
    from app.sub_agents.pulse.sub_agents.writer.agent import PulseItem

    card = _bare_card(
        [
            PulseItem(
                item_id="evt-1",
                title="1:1 with Sarah",
                why_now="Overdue",
                action="Go",
                url="https://example.com/evt-1",
            )
        ]
    )

    text = render_text(card)

    assert "1:1 with Sarah (https://example.com/evt-1)" in text


def test_build_card_carries_delivery_id_and_focus_item_types(pg_session):
    result = _trigger_result(pg_session, "normal_day")

    card = build_card(result)

    assert card.delivery_id == result.delivery_id
    assert card.delivery_id is not None
    for item in card.focus:
        assert card.focus_item_types[item.item_id] in ("event", "work_item", "message")


def test_render_blocks_adds_feedback_buttons_per_focus_item():
    from app.sub_agents.pulse.sub_agents.writer.agent import PulseItem

    card = _bare_card(
        [PulseItem(item_id="evt-1", title="1:1 with Sarah", why_now="Overdue", action="Go")],
        delivery_id="pd-1",
        focus_item_types={"evt-1": "event"},
    )

    blocks = render_blocks(card)
    actions_blocks = [
        b
        for b in blocks
        if b.get("type") == "actions" and b.get("block_id") == "pulse_feedback_evt-1"
    ]

    assert len(actions_blocks) == 1
    up, down = actions_blocks[0]["elements"]
    assert up["action_id"] == FEEDBACK_UP_ACTION_ID
    assert down["action_id"] == FEEDBACK_DOWN_ACTION_ID
    assert parse_feedback_value(up["value"]) == ("pd-1", "event", "evt-1")
    assert parse_feedback_value(down["value"]) == ("pd-1", "event", "evt-1")


def test_render_blocks_omits_feedback_buttons_without_a_delivery_id():
    from app.sub_agents.pulse.sub_agents.writer.agent import PulseItem

    card = _bare_card(
        [PulseItem(item_id="evt-1", title="1:1 with Sarah", why_now="Overdue", action="Go")],
        delivery_id=None,
    )

    blocks = render_blocks(card)

    assert not [b for b in blocks if b.get("block_id") == "pulse_feedback_evt-1"]


def test_render_blocks_is_json_serializable(pg_session):
    import json

    result = _trigger_result(pg_session, "normal_day")
    card = build_card(result)

    blocks = render_blocks(card)

    assert isinstance(blocks, list)
    json.dumps(blocks)  # must not raise


def test_render_text_never_shows_markdown_or_blockkit_syntax(pg_session):
    result = _trigger_result(pg_session, "normal_day")
    card = build_card(result)

    text = render_text(card)

    assert "```" not in text
    assert '"type": "section"' not in text


def test_day_times_are_shown_in_the_owners_local_timezone(pg_session):
    # normal_day.yaml's evt-1 starts_at "2026-08-07T09:00:00+02:00" (Paris).
    # Postgres timestamptz round-trips this back tagged UTC — 07:00 — so
    # naively formatting starts_at without converting to owner_tz first
    # would print the wrong wall-clock time on the card.
    result = _trigger_result(pg_session, "normal_day")
    card = build_card(result)

    evt1 = next(e for e in card.day if e.title == "1:1 with Sarah")
    assert evt1.starts_at.strftime("%H:%M") == "09:00"


def test_personalization_note_is_none_for_empty_weights():
    assert _personalization_note({}) is None


def test_personalization_note_is_none_when_every_weight_is_neutral():
    assert _personalization_note({"event": 1.0, "work_item": 1.0, "message": 1.0}) is None


def test_personalization_note_is_none_below_the_threshold():
    # 2% drift — real, but not worth mentioning (PERSONALIZATION_NOTE_THRESHOLD is 5%).
    assert _personalization_note({"event": 1.0, "work_item": 1.02, "message": 1.0}) is None


def test_personalization_note_mentions_the_type_with_the_largest_drift():
    note = _personalization_note({"event": 1.0, "work_item": 1.24, "message": 1.05})

    assert note is not None
    assert "work items" in note
    assert "24%" in note
    assert "higher" in note


def test_personalization_note_says_lower_for_a_negative_drift():
    note = _personalization_note({"event": 1.0, "work_item": 1.0, "message": 0.7})

    assert note is not None
    assert "messages" in note
    assert "30%" in note
    assert "lower" in note


def test_render_blocks_includes_the_personalization_note_when_present():
    from app.sub_agents.pulse.sub_agents.writer.agent import PulseItem

    card = PulseCard(
        focus=[PulseItem(item_id="evt-1", title="Standup", why_now="Now", action="Go")],
        day=[],
        owed=[],
        suggested_focus=None,
        degradation_line=None,
        since_note=None,
        shown_count=1,
        total_count=1,
        footer="prompt_ids=[] context_hash=x",
        personalization_note="🧠 Weighting work items 24% higher than usual, based on your recent 👍/👎.",
    )

    blocks = render_blocks(card)
    context_texts = [
        el["text"]
        for b in blocks
        if b["type"] == "context"
        for el in b["elements"]
    ]

    assert any("Weighting work items 24% higher" in t for t in context_texts)


def test_render_blocks_omits_the_personalization_note_when_absent(pg_session):
    result = _trigger_result(pg_session, "normal_day")
    card = build_card(result)

    assert card.personalization_note is None
    blocks = render_blocks(card)
    context_texts = [
        el["text"]
        for b in blocks
        if b["type"] == "context"
        for el in b["elements"]
    ]

    assert not any("Weighting" in t for t in context_texts)


def test_render_text_includes_the_personalization_note_when_present():
    from app.sub_agents.pulse.sub_agents.writer.agent import PulseItem

    card = PulseCard(
        focus=[PulseItem(item_id="evt-1", title="Standup", why_now="Now", action="Go")],
        day=[],
        owed=[],
        suggested_focus=None,
        degradation_line=None,
        since_note=None,
        shown_count=1,
        total_count=1,
        footer="prompt_ids=[] context_hash=x",
        personalization_note="🧠 Weighting work items 24% higher than usual, based on your recent 👍/👎.",
    )

    text = render_text(card)

    assert "Weighting work items 24% higher" in text
