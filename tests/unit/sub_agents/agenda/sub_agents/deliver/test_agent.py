import datetime
import json

from app.agenda.scope import resolve_pair_scope
from app.agenda.store import add_manual_note, update_agenda_item
from app.core.clock import FrozenClock
from app.sub_agents.agenda.sub_agents.deliver.agent import (
    PROMPT_ID,
    PhraseOutput,
    _build_items_json_callback,
    assemble_summary,
    phrase_agent,
)

NOW = datetime.datetime(2026, 8, 14, tzinfo=datetime.UTC)


class _FakeCallbackContext:
    def __init__(self, state):
        self.state = state


def test_assemble_summary_applies_phrased_text_then_builds_payload():
    items = [
        {
            "id": "item-1",
            "text": "unblock the CI flake",
            "source": "jira",
            "visibility": "shared",
            "status": "open",
        }
    ]
    phrased = {"item-1": "Unblock the flaky CI job (JIRA)"}

    result = assemble_summary(items, [], phrased)

    editable = next(c for c in result["components"] if c["type"] == "editable_text")
    assert editable["text"] == "Unblock the flaky CI job (JIRA)"


def test_assemble_summary_falls_back_to_raw_text_when_unphrased():
    items = [
        {
            "id": "item-1",
            "text": "unblock the CI flake",
            "source": "jira",
            "visibility": "shared",
            "status": "open",
        }
    ]

    result = assemble_summary(items, [], {})

    editable = next(c for c in result["components"] if c["type"] == "editable_text")
    assert editable["text"] == "unblock the CI flake"


def test_assemble_summary_only_rewrites_matching_item_ids():
    items = [
        {
            "id": "item-1",
            "text": "raw one",
            "source": "jira",
            "visibility": "shared",
            "status": "open",
        },
        {
            "id": "item-2",
            "text": "raw two",
            "source": "slack",
            "visibility": "shared",
            "status": "open",
        },
    ]
    phrased = {"item-1": "Phrased one"}

    result = assemble_summary(items, [], phrased)

    editables = {
        c["item_id"]: c["text"]
        for c in result["components"]
        if c["type"] == "editable_text"
    }
    assert editables["item-1"] == "Phrased one"
    assert editables["item-2"] == "raw two"


def test_assemble_summary_preserves_other_item_fields():
    items = [
        {
            "id": "item-1",
            "text": "raw text",
            "source": "jira",
            "visibility": "manager_only",
            "status": "open",
        }
    ]
    phrased = {"item-1": "Phrased text"}

    result = assemble_summary(items, [], phrased)

    toggle = next(c for c in result["components"] if c["type"] == "visibility_toggle")
    assert toggle["current"] == "manager_only"
    editable = next(c for c in result["components"] if c["type"] == "editable_text")
    assert editable["source"] == "jira"


def test_assemble_summary_does_not_mutate_input_items():
    items = [
        {
            "id": "item-1",
            "text": "raw text",
            "source": "jira",
            "visibility": "shared",
            "status": "open",
        }
    ]
    phrased = {"item-1": "Phrased text"}

    assemble_summary(items, [], phrased)

    assert items[0]["text"] == "raw text"


def test_assemble_summary_passes_pending_consent_items_through():
    pending = [{"id": "pc-1", "text": "consent needed"}]

    result = assemble_summary([], pending, {})

    consent = [c for c in result["components"] if c["type"] == "consent_card"]
    assert len(consent) == 1
    assert consent[0]["item_id"] == "pc-1"


def test_phrase_output_holds_id_to_text_mapping():
    output = PhraseOutput(phrased={"item-1": "Phrased text"})

    assert output.phrased["item-1"] == "Phrased text"


def test_phrase_agent_uses_structured_output():
    assert phrase_agent.output_schema is PhraseOutput
    assert phrase_agent.output_key == "phrase_result"


def test_phrase_agent_runs_at_temperature_zero():
    config = phrase_agent.generate_content_config
    assert config.temperature == 0


def test_phrase_agent_name():
    assert phrase_agent.name == "agenda_phrase"


def test_prompt_id_is_stable():
    assert PROMPT_ID == "agenda_deliver.v1"


def test_build_items_json_callback_populates_items_json_from_the_live_agenda(
    db_session, make_pair
):
    """The gap this fixes: phrase_agent's instruction ends with a literal
    {items_json} template placeholder that nothing ever populated. This
    callback (attached as phrase_agent's before_agent_callback, per
    invocation, since it closes over pair_scope) is what's supposed to
    populate it from the real agenda, right before phrase_agent's turn."""
    pair = make_pair()
    pair_scope = resolve_pair_scope(
        db_session, pair.report_user_id, pair.report_user_id
    )
    clock = FrozenClock(at=NOW)
    item = add_manual_note(pair_scope, pair.report_user_id, "raw text", None, clock)

    callback = _build_items_json_callback(pair_scope)
    state = {}
    callback(_FakeCallbackContext(state))

    assert "items_json" in state
    items = json.loads(state["items_json"])
    assert items == [
        {
            "id": item.id,
            "text": "raw text",
            "source": item.source,
            "visibility": item.visibility,
            "status": item.status,
        }
    ]


def test_build_items_json_callback_excludes_pending_consent_items(
    db_session, make_pair
):
    """pending_consent items get a separate consent_card treatment in
    payload.py (see run_post_meeting_flow's own post-hoc split) — they
    were never meant to reach phrase_agent for rewriting."""
    pair = make_pair()
    pair_scope = resolve_pair_scope(
        db_session, pair.report_user_id, pair.report_user_id
    )
    clock = FrozenClock(at=NOW)
    open_item = add_manual_note(
        pair_scope, pair.report_user_id, "open item", None, clock
    )
    consent_item = add_manual_note(
        pair_scope, pair.report_user_id, "needs consent", None, clock
    )
    update_agenda_item(
        pair_scope,
        consent_item.id,
        {"status": "pending_consent"},
        pair.report_user_id,
        clock,
    )

    callback = _build_items_json_callback(pair_scope)
    state = {}
    callback(_FakeCallbackContext(state))

    ids = {item["id"] for item in json.loads(state["items_json"])}
    assert ids == {open_item.id}
