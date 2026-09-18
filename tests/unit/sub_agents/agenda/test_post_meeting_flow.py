import datetime
import json
from unittest.mock import patch

from google.adk.agents import SequentialAgent

from app.agenda.scope import resolve_pair_scope
from app.agenda.store import add_manual_note, mark_resolved, update_agenda_item
from app.core.clock import FrozenClock
from app.core.scope import OwnerScope
from app.sub_agents.agenda.agent import run_post_meeting_flow
from app.sub_agents.agenda.sub_agents.capture.agent import capture_agent
from app.sub_agents.agenda.sub_agents.deliver.agent import phrase_agent


class _FakeCallbackContext:
    def __init__(self, state):
        self.state = state


NOW = datetime.datetime(2026, 8, 14, tzinfo=datetime.UTC)

FAKE_STATE = {
    "capture_result": {
        "transcript_text": "Discussed the roadmap.",
        "source": "transcript",
    },
    "synthesize_result": {
        "decisions": [],
        "commitments": [],
        "focus_points": ["roadmap"],
    },
    "phrase_result": {"phrased": {}},
}


def test_run_post_meeting_flow_returns_an_a2ui_payload(db_session, make_pair):
    pair = make_pair()
    pair_scope = resolve_pair_scope(
        db_session, pair.report_user_id, pair.report_user_id
    )
    owner_scope = OwnerScope(owner_user_id=pair.report_user_id, session=db_session)

    with patch("app.sub_agents.agenda.agent.run_agent_sync", return_value=FAKE_STATE):
        result = run_post_meeting_flow(
            "meeting-1", pair_scope, owner_scope, FrozenClock(at=NOW)
        )

    assert "components" in result


def test_meeting_end_triggered_by_manager_still_sees_report_only_items(
    db_session, make_pair
):
    """Composition-review finding: agenda_router.py's POST /webhooks/
    agenda-trigger lets either party set acting_user_id. If run_post_
    meeting_flow read the agenda through pair_scope as given, a
    manager-triggered run would be blind to report_only items (is_
    visible_to filters them for a non-report actor) with no way to
    dedup against them. Internal dedup/synthesis must always see the
    full report-perspective agenda regardless of who triggered it."""
    pair = make_pair()
    report_scope = resolve_pair_scope(
        db_session, pair.report_user_id, pair.report_user_id
    )
    manager_triggered_scope = resolve_pair_scope(
        db_session, pair.report_user_id, pair.manager_user_id
    )
    owner_scope = OwnerScope(owner_user_id=pair.report_user_id, session=db_session)
    clock = FrozenClock(at=NOW)

    add_manual_note(
        report_scope, pair.report_user_id, "private report note", "report_only", clock
    )

    with patch("app.sub_agents.agenda.agent.run_agent_sync", return_value=FAKE_STATE):
        result = run_post_meeting_flow(
            "meeting-1", manager_triggered_scope, owner_scope, clock
        )

    texts = [
        c.get("text") for c in result["components"] if c.get("type") == "editable_text"
    ]
    assert "private report note" in texts


def test_manager_only_item_still_gets_phrased_when_report_triggers_meeting_end(
    db_session, make_pair
):
    """Re-review finding: the fix above (always read the full agenda
    internally, via get_full_agenda) must be symmetric. A single
    report-perspective read would fix report_only dedup blindness but
    leave manager_only items permanently unphrased (excluded from
    phrase_agent's items_json) whenever a report-triggered run is the
    one doing the phrasing."""
    pair = make_pair()
    manager_scope = resolve_pair_scope(
        db_session, pair.report_user_id, pair.manager_user_id
    )
    report_triggered_scope = resolve_pair_scope(
        db_session, pair.report_user_id, pair.report_user_id
    )
    owner_scope = OwnerScope(owner_user_id=pair.report_user_id, session=db_session)
    clock = FrozenClock(at=NOW)

    manager_item = add_manual_note(
        manager_scope, pair.manager_user_id, "raw manager note", "manager_only", clock
    )

    fake_state = {
        **FAKE_STATE,
        "phrase_result": {"phrased": {manager_item.id: "polished manager note"}},
    }

    with patch("app.sub_agents.agenda.agent.run_agent_sync", return_value=fake_state):
        result = run_post_meeting_flow(
            "meeting-1", report_triggered_scope, owner_scope, clock
        )

    texts = [
        c.get("text") for c in result["components"] if c.get("type") == "editable_text"
    ]
    assert "polished manager note" in texts
    assert "raw manager note" not in texts


def test_resolved_item_never_appears_in_the_returned_payload(db_session, make_pair):
    """The items filter used to be status != "pending_consent", which
    also let resolved items through — they'd keep showing up as active,
    editable components in the returned payload forever."""
    pair = make_pair()
    pair_scope = resolve_pair_scope(
        db_session, pair.report_user_id, pair.report_user_id
    )
    owner_scope = OwnerScope(owner_user_id=pair.report_user_id, session=db_session)
    clock = FrozenClock(at=NOW)

    item = add_manual_note(
        pair_scope, pair.report_user_id, "will be resolved", "shared", clock
    )
    mark_resolved(pair_scope, item.id, pair.report_user_id, clock)

    with patch("app.sub_agents.agenda.agent.run_agent_sync", return_value=FAKE_STATE):
        result = run_post_meeting_flow("meeting-1", pair_scope, owner_scope, clock)

    item_ids = {c.get("item_id") for c in result["components"] if "item_id" in c}
    assert item.id not in item_ids


def test_explicit_transcript_text_is_wired_onto_the_capture_clone(
    db_session, make_pair
):
    """Real-world finding: without an explicit transcript, Capture's
    fetch_transcript tool has no real backing store and the whole
    pipeline silently produces zero agenda items (confirmed live against
    a real Gemini call). run_post_meeting_flow's transcript_text param
    must reach the actual cloned capture_agent's tool, not just get
    accepted and ignored."""
    pair = make_pair()
    pair_scope = resolve_pair_scope(
        db_session, pair.report_user_id, pair.report_user_id
    )
    owner_scope = OwnerScope(owner_user_id=pair.report_user_id, session=db_session)

    with patch(
        "app.sub_agents.agenda.agent.run_agent_sync", return_value=FAKE_STATE
    ) as mock_run:
        run_post_meeting_flow(
            "meeting-1",
            pair_scope,
            owner_scope,
            FrozenClock(at=NOW),
            transcript_text="We agreed to ship on Friday.",
        )

    wired_agent = mock_run.call_args[0][0]
    capture_clone = wired_agent.sub_agents[0]
    fetch_transcript_tool = capture_clone.tools[0]
    assert fetch_transcript_tool("any-meeting-id") == "We agreed to ship on Friday."


def test_kickoff_text_carries_meeting_id_to_the_llm_turn(db_session, make_pair):
    """capture_agent's own instruction template has no {meeting_id}
    placeholder (app/prompts/agenda_capture.v1.md), so meeting_id can only
    reach the LLM via the kickoff user turn. Assert it's actually there,
    not just present in initial_state (which the LLM never sees directly
    for a plain Agent's first turn)."""
    pair = make_pair()
    pair_scope = resolve_pair_scope(
        db_session, pair.report_user_id, pair.report_user_id
    )
    owner_scope = OwnerScope(owner_user_id=pair.report_user_id, session=db_session)

    with patch(
        "app.sub_agents.agenda.agent.run_agent_sync", return_value=FAKE_STATE
    ) as mock_run:
        run_post_meeting_flow(
            "meeting-xyz", pair_scope, owner_scope, FrozenClock(at=NOW)
        )

    _agent, _initial_state, kwargs = (
        mock_run.call_args[0][0],
        mock_run.call_args[0][1],
        mock_run.call_args[1],
    )
    kickoff_text = kwargs.get("kickoff_text", "")
    assert "meeting-xyz" in kickoff_text


def test_sequential_agent_wiring_is_capture_then_fresh_synthesize_then_phrase(
    db_session, make_pair
):
    pair = make_pair()
    pair_scope = resolve_pair_scope(
        db_session, pair.report_user_id, pair.report_user_id
    )
    owner_scope = OwnerScope(owner_user_id=pair.report_user_id, session=db_session)

    captured_agents = []

    def _capture_and_return(agent, *args, **kwargs):
        captured_agents.append(agent)
        return FAKE_STATE

    with patch(
        "app.sub_agents.agenda.agent.run_agent_sync", side_effect=_capture_and_return
    ):
        run_post_meeting_flow("meeting-1", pair_scope, owner_scope, FrozenClock(at=NOW))
        run_post_meeting_flow("meeting-2", pair_scope, owner_scope, FrozenClock(at=NOW))

    assert len(captured_agents) == 2
    for wired_agent in captured_agents:
        assert isinstance(wired_agent, SequentialAgent)
        assert len(wired_agent.sub_agents) == 4
        first, second, third, fourth = wired_agent.sub_agents
        # capture_agent/phrase_agent are shared singletons ADK's
        # SequentialAgent mutates on assembly (stamps parent_agent), so
        # the wired sub-agents are clones, not the singletons themselves
        # — identity would break on a second call. Same config, though.
        assert first.name == capture_agent.name
        assert first is not capture_agent
        assert second.name == "agenda_map_okrs"
        assert third.name == "agenda_synthesize"
        assert fourth.name == phrase_agent.name
        assert fourth is not phrase_agent

    # each call must build its own map_okrs/synthesize agent (their tools
    # close over this call's specific pair_scope/owner_scope/clock)
    # rather than reusing one instance across meetings.
    first_map_okrs = captured_agents[0].sub_agents[1]
    second_map_okrs = captured_agents[1].sub_agents[1]
    assert first_map_okrs is not second_map_okrs

    first_synthesize = captured_agents[0].sub_agents[2]
    second_synthesize = captured_agents[1].sub_agents[2]
    assert first_synthesize is not second_synthesize


def test_separates_pending_consent_items_from_the_main_agenda(db_session, make_pair):
    pair = make_pair()
    pair_scope = resolve_pair_scope(
        db_session, pair.report_user_id, pair.report_user_id
    )
    owner_scope = OwnerScope(owner_user_id=pair.report_user_id, session=db_session)
    clock = FrozenClock(at=NOW)

    open_item = add_manual_note(
        pair_scope, pair.report_user_id, "Open item", None, clock
    )
    consent_item = add_manual_note(
        pair_scope, pair.report_user_id, "Needs consent", None, clock
    )
    update_agenda_item(
        pair_scope,
        consent_item.id,
        {"status": "pending_consent"},
        pair.report_user_id,
        clock,
    )

    with patch("app.sub_agents.agenda.agent.run_agent_sync", return_value=FAKE_STATE):
        result = run_post_meeting_flow("meeting-1", pair_scope, owner_scope, clock)

    component_types_by_item = {}
    for component in result["components"]:
        if "item_id" not in component:
            continue  # e.g. the trailing {"type": "add_item"} component
        component_types_by_item.setdefault(component["item_id"], set()).add(
            component["type"]
        )

    assert component_types_by_item[open_item.id] == {
        "editable_text",
        "visibility_toggle",
    }
    assert component_types_by_item[consent_item.id] == {"consent_card"}


def test_uses_phrase_result_phrased_text_to_rewrite_item_text(db_session, make_pair):
    pair = make_pair()
    pair_scope = resolve_pair_scope(
        db_session, pair.report_user_id, pair.report_user_id
    )
    owner_scope = OwnerScope(owner_user_id=pair.report_user_id, session=db_session)
    clock = FrozenClock(at=NOW)

    item = add_manual_note(pair_scope, pair.report_user_id, "raw text", None, clock)

    state_with_phrasing = {
        **FAKE_STATE,
        "phrase_result": {"phrased": {item.id: "polished text"}},
    }

    with patch(
        "app.sub_agents.agenda.agent.run_agent_sync", return_value=state_with_phrasing
    ):
        result = run_post_meeting_flow("meeting-1", pair_scope, owner_scope, clock)

    editable = next(
        c
        for c in result["components"]
        if c["type"] == "editable_text" and c["item_id"] == item.id
    )
    assert editable["text"] == "polished text"


def test_missing_phrase_result_key_falls_back_to_raw_item_text(db_session, make_pair):
    """state.get("phrase_result", {}) must not blow up when the
    SequentialAgent's final state has no phrase_result key at all (e.g.
    phrase_agent failed to write it)."""
    pair = make_pair()
    pair_scope = resolve_pair_scope(
        db_session, pair.report_user_id, pair.report_user_id
    )
    owner_scope = OwnerScope(owner_user_id=pair.report_user_id, session=db_session)
    clock = FrozenClock(at=NOW)

    item = add_manual_note(pair_scope, pair.report_user_id, "raw text", None, clock)

    with patch(
        "app.sub_agents.agenda.agent.run_agent_sync",
        return_value={"capture_result": {}, "synthesize_result": {}},
    ):
        result = run_post_meeting_flow("meeting-1", pair_scope, owner_scope, clock)

    editable = next(
        c
        for c in result["components"]
        if c["type"] == "editable_text" and c["item_id"] == item.id
    )
    assert editable["text"] == "raw text"


def test_cloned_phrase_agent_gets_a_per_invocation_items_json_callback(
    db_session, make_pair
):
    """The functional gap this fixes: phrase_agent's instruction template
    references {items_json}, but nothing ever populated it. Assert the
    clone wired into the SequentialAgent carries a before_agent_callback
    that, when run, seeds items_json from this call's real pair_scope.

    Uses two separate run_post_meeting_flow calls against two distinct
    pairs/items to prove the callback is genuinely built fresh per call
    (closing over that call's own pair_scope), not memoized/shared — a
    single call can't distinguish "fresh per call" from "built once and
    happened to close over the right scope the first time" (e.g. an
    lru_cache'd factory)."""
    pair_one = make_pair()
    pair_scope_one = resolve_pair_scope(
        db_session, pair_one.report_user_id, pair_one.report_user_id
    )
    owner_scope_one = OwnerScope(
        owner_user_id=pair_one.report_user_id, session=db_session
    )
    clock = FrozenClock(at=NOW)
    item_one = add_manual_note(
        pair_scope_one, pair_one.report_user_id, "raw text one", None, clock
    )

    pair_two = make_pair()
    pair_scope_two = resolve_pair_scope(
        db_session, pair_two.report_user_id, pair_two.report_user_id
    )
    owner_scope_two = OwnerScope(
        owner_user_id=pair_two.report_user_id, session=db_session
    )
    item_two = add_manual_note(
        pair_scope_two, pair_two.report_user_id, "raw text two", None, clock
    )

    captured_agents = []

    def _capture_and_return(agent, *args, **kwargs):
        captured_agents.append(agent)
        return FAKE_STATE

    with patch(
        "app.sub_agents.agenda.agent.run_agent_sync", side_effect=_capture_and_return
    ):
        run_post_meeting_flow("meeting-1", pair_scope_one, owner_scope_one, clock)
        run_post_meeting_flow("meeting-2", pair_scope_two, owner_scope_two, clock)

    assert len(captured_agents) == 2

    phrase_clone_one = captured_agents[0].sub_agents[3]
    phrase_clone_two = captured_agents[1].sub_agents[3]
    assert phrase_clone_one.before_agent_callback is not None
    assert phrase_clone_two.before_agent_callback is not None
    assert (
        phrase_clone_one.before_agent_callback
        is not phrase_clone_two.before_agent_callback
    )

    state_one = {}
    phrase_clone_one.before_agent_callback(_FakeCallbackContext(state_one))
    items_one = json.loads(state_one["items_json"])
    assert [entry["id"] for entry in items_one] == [item_one.id]

    state_two = {}
    phrase_clone_two.before_agent_callback(_FakeCallbackContext(state_two))
    items_two = json.loads(state_two["items_json"])
    assert [entry["id"] for entry in items_two] == [item_two.id]
