from app.delivery.cards import build_dossier_card
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


def test_no_header_when_event_title_is_absent():
    blocks = build_dossier_card(_card())
    assert blocks[0]["text"]["text"].startswith("*Who:*")


def test_blockers_section_rendered_when_present():
    blocks = build_dossier_card(
        _card(blockers=["MENT-214 — Fix flaky roster test (blocked — Sarah), 3d"])
    )
    texts = [b["text"]["text"] for b in blocks if b.get("type") == "section"]
    assert any("Blockers" in t and "MENT-214" in t for t in texts)


def test_no_blockers_section_when_empty():
    blocks = build_dossier_card(_card(blockers=[]))
    texts = [b["text"]["text"] for b in blocks if b.get("type") == "section"]
    assert not any("Blockers" in t for t in texts)


def test_header_shows_the_event_title_and_time():
    blocks = build_dossier_card(
        _card(),
        event_title="Weekly 1:1 with Sam",
        event_starts_at="2026-08-24T14:05:00+00:00",
    )
    header = blocks[0]["text"]["text"]
    assert "Weekly 1:1 with Sam" in header
    assert "2:05 PM" in header
    assert blocks[1] == {"type": "divider"}
    # Who/why-now still follow the header, unchanged.
    assert blocks[2]["text"]["text"].startswith("*Who:*")


def test_header_omits_time_line_when_starts_at_is_absent():
    blocks = build_dossier_card(_card(), event_title="Weekly 1:1 with Sam")
    header = blocks[0]["text"]["text"]
    assert "Weekly 1:1 with Sam" in header
    assert "🕒" not in header


def test_header_falls_back_to_the_raw_string_on_an_unparseable_time():
    blocks = build_dossier_card(
        _card(), event_title="Weekly 1:1 with Sam", event_starts_at="not-a-real-date"
    )
    header = blocks[0]["text"]["text"]
    assert "not-a-real-date" in header


def test_talking_point_with_a_non_url_source_link_renders_as_plain_text():
    # Regression: AgendaItem.source_link is sometimes that row's own
    # internal UUID (app/agenda/store.py's append_ledger_item), not a
    # clickable URL — synthesize_dossier correctly keeps these carryover
    # points, but build_dossier_card used to blindly linkify source_link
    # regardless, rendering a broken "(<uuid|source>)" in Slack.
    card = _card(
        talking_points=[
            TalkingPoint(
                text="Bob will finalize migration tests by Thursday",
                source_link="3311b5bc-97c2-49a2-9f75-5b8303477024",
            )
        ]
    )
    blocks = build_dossier_card(card)
    talking_points_text = blocks[2]["text"]["text"]
    assert "• Bob will finalize migration tests by Thursday" in talking_points_text
    assert "3311b5bc" not in talking_points_text
    assert "<" not in talking_points_text


def test_talking_point_with_no_source_link_renders_as_plain_text():
    card = _card(
        talking_points=[TalkingPoint(text="No link on this one", source_link=None)]
    )
    blocks = build_dossier_card(card)
    talking_points_text = blocks[2]["text"]["text"]
    assert "• No link on this one" in talking_points_text
    assert "None" not in talking_points_text
