import json

import pytest

from app.salience.pulse.types import ScoredItem
from app.sub_agents.pulse.sub_agents.ranker.agent import (
    PROMPT_ID,
    RankerOutput,
    _default_previous_critique,
    fallback_rank,
    rank,
    ranker_agent,
    run_ranker_agent,
)
from tests.fixture_loading import load_ranker_fixture, scored_items_from_shortlist

_SHORTLIST = [
    ScoredItem(
        item_id="evt-1",
        item_type="event",
        score=0.9,
        score_terms={"overdue": True, "person_waiting": "Sarah"},
        candidate_focus=True,
    ),
    ScoredItem(
        item_id="MENT-201",
        item_type="work_item",
        score=0.4,
        score_terms={"due_today": True},
        candidate_focus=True,
    ),
]


def test_run_ranker_agent_seeds_state_with_the_shortlist_as_json(monkeypatch):
    captured = {}

    def fake_run_agent_sync(agent, initial_state, **kwargs):
        captured["agent"] = agent
        captured["state"] = initial_state
        return {
            "ranker_result": {
                "ordered_item_ids": ["evt-1", "MENT-201"],
                "rationale": {"evt-1": "overdue", "MENT-201": "due today"},
            }
        }

    monkeypatch.setattr(
        "app.sub_agents.pulse.sub_agents.ranker.agent.run_agent_sync",
        fake_run_agent_sync,
    )

    result = run_ranker_agent(_SHORTLIST)

    assert captured["agent"] is ranker_agent
    seeded = json.loads(captured["state"]["ranker_input_json"])
    assert seeded == [
        {"item_id": "evt-1", "score": 0.9, "score_terms": _SHORTLIST[0].score_terms},
        {
            "item_id": "MENT-201",
            "score": 0.4,
            "score_terms": _SHORTLIST[1].score_terms,
        },
    ]
    assert isinstance(result, RankerOutput)
    assert result.ordered_item_ids == ["evt-1", "MENT-201"]


def test_run_ranker_agent_raises_on_failure_rather_than_falling_back(monkeypatch):
    def _boom(agent, initial_state, **kwargs):
        raise RuntimeError("network error")

    monkeypatch.setattr(
        "app.sub_agents.pulse.sub_agents.ranker.agent.run_agent_sync", _boom
    )

    try:
        run_ranker_agent(_SHORTLIST)
        raised = False
    except RuntimeError:
        raised = True

    assert raised is True


FIXTURE_NAMES = ["normal_day", "low_signal_day", "tied_items"]


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_fallback_rank_matches_expected_ordered_ids(name):
    fixture = load_ranker_fixture(name)
    shortlist = scored_items_from_shortlist(fixture["shortlist"])

    result = fallback_rank(shortlist)

    assert result.ordered_item_ids == fixture["expected_ordered_ids"]


def test_fallback_rank_never_exceeds_three():
    fixture = load_ranker_fixture("normal_day")
    shortlist = scored_items_from_shortlist(fixture["shortlist"])

    result = fallback_rank(shortlist)

    assert len(result.ordered_item_ids) <= 3


def test_fallback_rank_has_a_rationale_per_ordered_item():
    fixture = load_ranker_fixture("normal_day")
    shortlist = scored_items_from_shortlist(fixture["shortlist"])

    result = fallback_rank(shortlist)

    for item_id in result.ordered_item_ids:
        assert item_id in result.rationale
        assert result.rationale[item_id]  # non-empty


def test_fallback_rank_prompt_id_is_stable():
    fixture = load_ranker_fixture("tied_items")
    shortlist = scored_items_from_shortlist(fixture["shortlist"])

    result = fallback_rank(shortlist)

    assert result.prompt_id == PROMPT_ID


def test_rank_falls_back_when_model_call_raises():
    fixture = load_ranker_fixture("normal_day")
    shortlist = scored_items_from_shortlist(fixture["shortlist"])

    def broken_model(shortlist, context):
        raise RuntimeError("no API key configured")

    result = rank(shortlist, context=None, model_call=broken_model)

    assert result.ordered_item_ids == fixture["expected_ordered_ids"]


def test_rank_with_no_model_call_uses_fallback():
    fixture = load_ranker_fixture("normal_day")
    shortlist = scored_items_from_shortlist(fixture["shortlist"])

    result = rank(shortlist, context=None)

    assert result.ordered_item_ids == fixture["expected_ordered_ids"]


class _FakeCallbackContext:
    def __init__(self, state):
        self.state = state


def test_default_previous_critique_seeds_empty_list_when_absent():
    state = {}

    _default_previous_critique(_FakeCallbackContext(state))

    assert state["previous_critique_json"] == "[]"


def test_default_previous_critique_leaves_a_real_critique_alone():
    """A pulse_agent loop retry seeds a real critique before ranker_agent's
    turn runs again — this callback must not clobber it."""
    state = {"previous_critique_json": '[{"item_id": "evt-1", "reason": "x"}]'}

    _default_previous_critique(_FakeCallbackContext(state))

    assert state["previous_critique_json"] == '[{"item_id": "evt-1", "reason": "x"}]'
