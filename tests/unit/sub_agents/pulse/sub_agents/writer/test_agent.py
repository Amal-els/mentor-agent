import json

from app.sub_agents.pulse.sub_agents.writer.agent import (
    PROMPT_ID,
    PulseItem,
    WriterInputItem,
    WriterOutput,
    _prepare_writer_input,
    fallback_render,
    run_writer_agent,
    write,
    writer_agent,
)


def _item(item_id, title, score_terms):
    return WriterInputItem(
        item_id=item_id,
        title=title,
        score_terms=score_terms,
        rationale="scratch",
    )


class _FakeWriterInputItem:
    def __init__(self, item_id, title, score_terms, rationale):
        self.item_id = item_id
        self.title = title
        self.score_terms = score_terms
        self.rationale = rationale


def test_run_writer_agent_seeds_state_with_items_as_json(monkeypatch):
    captured = {}

    def fake_run_agent_sync(agent, initial_state, **kwargs):
        captured["agent"] = agent
        captured["state"] = initial_state
        return {
            "writer_result": {
                "items": [
                    {
                        "item_id": "evt-1",
                        "title": "1:1 with Sarah",
                        "why_now": "Overdue",
                        "action": "Deliver this today",
                    }
                ]
            }
        }

    monkeypatch.setattr(
        "app.sub_agents.pulse.sub_agents.writer.agent.run_agent_sync",
        fake_run_agent_sync,
    )

    items = [
        _FakeWriterInputItem("evt-1", "1:1 with Sarah", {"overdue": True}, "overdue")
    ]
    result = run_writer_agent(items)

    assert captured["agent"] is writer_agent
    seeded = json.loads(captured["state"]["writer_input_json"])
    assert seeded == [
        {
            "item_id": "evt-1",
            "title": "1:1 with Sarah",
            "score_terms": {"overdue": True},
            "rationale": "overdue",
            "previous_critique": "",
        }
    ]
    assert isinstance(result, WriterOutput)
    assert result.items[0].item_id == "evt-1"


def test_run_writer_agent_raises_on_failure(monkeypatch):
    def _boom(agent, initial_state, **kwargs):
        raise RuntimeError("malformed response")

    monkeypatch.setattr(
        "app.sub_agents.pulse.sub_agents.writer.agent.run_agent_sync", _boom
    )

    try:
        run_writer_agent([_FakeWriterInputItem("evt-1", "t", {}, "r")])
        raised = False
    except RuntimeError:
        raised = True

    assert raised is True


class _FakeCallbackContext:
    def __init__(self, state):
        self.state = state


def test_prepare_writer_input_is_a_noop_when_already_present():
    state = {"writer_input_json": '[{"item_id": "already-built"}]'}

    _prepare_writer_input(_FakeCallbackContext(state))

    assert state["writer_input_json"] == '[{"item_id": "already-built"}]'


def test_prepare_writer_input_merges_ranker_result_with_pre_seeded_titles():
    state = {
        "ranker_result": {
            "ordered_item_ids": ["evt-1", "MENT-201"],
            "rationale": {"evt-1": "overdue", "MENT-201": "due today"},
        },
        "item_titles_json": json.dumps(
            {"evt-1": "1:1 with Sarah", "MENT-201": "Fix flaky test"}
        ),
        "item_score_terms_json": json.dumps(
            {"evt-1": {"overdue": True}, "MENT-201": {"due_today": True}}
        ),
    }

    _prepare_writer_input(_FakeCallbackContext(state))

    built = json.loads(state["writer_input_json"])
    assert built == [
        {
            "item_id": "evt-1",
            "title": "1:1 with Sarah",
            "score_terms": {"overdue": True},
            "rationale": "overdue",
            "previous_critique": "",
        },
        {
            "item_id": "MENT-201",
            "title": "Fix flaky test",
            "score_terms": {"due_today": True},
            "rationale": "due today",
            "previous_critique": "",
        },
    ]


def test_prepare_writer_input_merges_previous_critique_by_item_id():
    state = {
        "ranker_result": {
            "ordered_item_ids": ["evt-1", "MENT-201"],
            "rationale": {"evt-1": "overdue", "MENT-201": "due today"},
        },
        "item_titles_json": json.dumps(
            {"evt-1": "1:1 with Sarah", "MENT-201": "Fix flaky test"}
        ),
        "item_score_terms_json": json.dumps(
            {"evt-1": {"overdue": True}, "MENT-201": {"due_today": True}}
        ),
        "previous_critique_json": json.dumps(
            [{"item_id": "evt-1", "reason": "invented a name"}]
        ),
    }

    _prepare_writer_input(_FakeCallbackContext(state))

    built = json.loads(state["writer_input_json"])
    assert built[0]["previous_critique"] == "invented a name"
    assert built[1]["previous_critique"] == ""  # MENT-201 wasn't flagged


def test_prepare_writer_input_rebuilds_on_a_second_loop_iteration():
    """The exact staleness bug this callback must not have: state persists
    across pulse_agent's LoopAgent iterations, so a stale writer_input_json
    left over from iteration 1 must be overwritten, not skipped, once a
    fresh ranker_result lands for iteration 2."""
    state = {
        "writer_input_json": json.dumps([{"item_id": "stale-from-iteration-1"}]),
        "ranker_result": {
            "ordered_item_ids": ["evt-1"],
            "rationale": {"evt-1": "overdue"},
        },
        "item_titles_json": json.dumps({"evt-1": "1:1 with Sarah"}),
        "item_score_terms_json": json.dumps({"evt-1": {"overdue": True}}),
    }

    _prepare_writer_input(_FakeCallbackContext(state))

    built = json.loads(state["writer_input_json"])
    assert [item["item_id"] for item in built] == ["evt-1"]


def test_fallback_render_preserves_order():
    items = [
        _item(
            "evt-1", "1:1 with Sarah", {"due_today": True, "person_waiting": "Sarah"}
        ),
        _item(
            "MENT-214", "Fix flaky test", {"overdue": True, "person_waiting": "Sarah"}
        ),
    ]

    drafted = fallback_render(items)

    assert [d.item_id for d in drafted] == ["evt-1", "MENT-214"]


def test_fallback_render_why_now_traces_to_score_terms():
    items = [
        _item("evt-1", "1:1 with Sarah", {"due_today": True, "person_waiting": "Sarah"})
    ]

    drafted = fallback_render(items)

    assert "Sarah" in drafted[0].why_now
    assert (
        "due" in drafted[0].why_now.lower() or "overdue" in drafted[0].why_now.lower()
    )


def test_fallback_render_never_invents_a_name():
    items = [
        _item("evt-1", "Team standup", {"due_today": True, "person_waiting": None})
    ]

    drafted = fallback_render(items)

    # no person_waiting in score_terms -> no name may appear in why_now
    assert "Sarah" not in drafted[0].why_now
    assert "Marc" not in drafted[0].why_now


def test_fallback_render_title_passes_through_unchanged():
    items = [_item("evt-1", "1:1 with Sarah", {"due_today": True})]

    drafted = fallback_render(items)

    assert drafted[0].title == "1:1 with Sarah"


def test_fallback_render_produces_an_action_for_every_item():
    items = [
        _item("evt-1", "1:1 with Sarah", {"due_today": True}),
        _item("MENT-214", "Fix flaky test", {"overdue": True}),
    ]

    drafted = fallback_render(items)

    assert all(d.action for d in drafted)


def test_fallback_render_flags_a_repeated_item():
    items = [
        _item(
            "MENT-214",
            "Fix flaky test",
            {"overdue": True, "person_waiting": "Sarah", "is_new_since_last_pulse": False},
        )
    ]

    drafted = fallback_render(items)

    assert "still open" in drafted[0].why_now.lower()
    assert "since your last pulse" in drafted[0].why_now.lower()
    # the underlying fact is still there, just reframed as carried over
    assert "Sarah" in drafted[0].why_now


def test_fallback_render_does_not_flag_a_new_or_event_item():
    items = [
        _item(
            "MENT-215",
            "New bug report",
            {"overdue": True, "is_new_since_last_pulse": True},
        ),
        # events never carry the term at all — key absent, not False
        _item("evt-1", "1:1 with Sarah", {"due_today": True}),
    ]

    drafted = fallback_render(items)

    assert "still open" not in drafted[0].why_now.lower()
    assert "still open" not in drafted[1].why_now.lower()


def test_write_falls_back_when_model_call_raises():
    items = [
        _item("evt-1", "1:1 with Sarah", {"due_today": True, "person_waiting": "Sarah"})
    ]

    def broken_model(items):
        raise RuntimeError("no API key configured")

    drafted = write(items, model_call=broken_model)

    assert [d.item_id for d in drafted] == ["evt-1"]


def test_write_rejects_a_model_that_reorders_items():
    items = [
        _item("evt-1", "1:1 with Sarah", {"due_today": True}),
        _item("MENT-214", "Fix flaky test", {"overdue": True}),
    ]

    def reordering_model(items):
        return [
            PulseItem(item_id="MENT-214", title="x", why_now="y", action="z"),
            PulseItem(item_id="evt-1", title="x", why_now="y", action="z"),
        ]

    drafted = write(items, model_call=reordering_model)

    # rejected — must fall back to the order-preserving template render
    assert [d.item_id for d in drafted] == ["evt-1", "MENT-214"]


def test_write_rejects_a_model_that_drops_an_item():
    items = [
        _item("evt-1", "1:1 with Sarah", {"due_today": True}),
        _item("MENT-214", "Fix flaky test", {"overdue": True}),
    ]

    def dropping_model(items):
        return [PulseItem(item_id="evt-1", title="x", why_now="y", action="z")]

    drafted = write(items, model_call=dropping_model)

    assert [d.item_id for d in drafted] == ["evt-1", "MENT-214"]


def test_prompt_id_constant_matches_prompt_file_name():
    assert PROMPT_ID == "pulse_writer.v1"
