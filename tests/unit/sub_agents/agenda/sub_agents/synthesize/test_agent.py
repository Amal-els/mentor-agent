import datetime
import uuid

import pydantic
import pytest
from google.genai import types as genai_types

from app.agenda.scope import resolve_pair_scope
from app.agenda.store import append_agenda_item
from app.core.clock import FrozenClock
from app.core.scope import OwnerScope
from app.sub_agents.agenda.sub_agents.synthesize.agent import (
    PROMPT_ID,
    AgendaItemInput,
    LedgerItemInput,
    SynthesizeOutput,
    _prepare_related_key_results,
    build_synthesize_agent,
    synthesize_tools,
)

NOW = datetime.datetime(2026, 8, 14, tzinfo=datetime.UTC)


def _scopes(db_session, make_pair):
    pair = make_pair()
    pair_scope = resolve_pair_scope(
        db_session, pair.report_user_id, pair.report_user_id
    )
    owner_scope = OwnerScope(owner_user_id=pair.report_user_id, session=db_session)
    return pair, pair_scope, owner_scope


def test_get_agenda_tool_returns_serializable_items(db_session, make_pair):
    pair = make_pair()
    pair_scope = resolve_pair_scope(
        db_session, pair.report_user_id, pair.report_user_id
    )
    owner_scope = OwnerScope(owner_user_id=pair.report_user_id, session=db_session)
    append_agenda_item(
        pair_scope,
        {
            "text": "existing item",
            "source": "manual",
            "source_link": None,
            "visibility": "shared",
            "created_by_user_id": pair.report_user_id,
            "created_by_role": "report",
        },
        FrozenClock(at=NOW),
    )
    tools = synthesize_tools(pair_scope, owner_scope, FrozenClock(at=NOW))
    get_agenda_tool = tools[0]

    result = get_agenda_tool()

    assert isinstance(result, list)
    assert result[0]["text"] == "existing item"
    assert "source_link" in result[0]
    # every value must be a plain, JSON-serializable type -- an ORM row
    # can't be handed back to the LLM tool caller.
    for item in result:
        for value in item.values():
            assert value is None or isinstance(value, (str, int, bool))


def test_append_agenda_item_tool_dedups_by_source_link(db_session, make_pair):
    pair = make_pair()
    pair_scope = resolve_pair_scope(
        db_session, pair.report_user_id, pair.report_user_id
    )
    owner_scope = OwnerScope(owner_user_id=pair.report_user_id, session=db_session)
    tools = synthesize_tools(pair_scope, owner_scope, FrozenClock(at=NOW))
    append_agenda_item_tool = tools[2]

    first = append_agenda_item_tool(
        text="blocked on review",
        source="jira",
        source_link="JIRA-9",
        visibility="shared",
        created_by_role="report",
    )
    second = append_agenda_item_tool(
        text="blocked on review",
        source="jira",
        source_link="JIRA-9",
        visibility="shared",
        created_by_role="report",
    )

    assert first["id"] == second["id"]
    assert second["surfaced_count"] == 1


def test_append_agenda_items_tool_batches_multiple_items_in_one_call(
    db_session, make_pair
):
    pair, pair_scope, owner_scope = _scopes(db_session, make_pair)
    tools = synthesize_tools(pair_scope, owner_scope, FrozenClock(at=NOW))
    append_agenda_items_tool = tools[2]

    results = append_agenda_items_tool(
        items=[
            AgendaItemInput(
                text="review the Q3 spend report",
                source="meeting_synthesis",
                created_by_role="report",
                visibility="shared",
            ),
            AgendaItemInput(
                text="check pipeline failure rate progress",
                source="meeting_synthesis",
                created_by_role="report",
                visibility="shared",
            ),
        ]
    )

    assert len(results) == 2
    assert results[0]["text"] == "review the Q3 spend report"
    assert results[1]["text"] == "check pipeline failure rate progress"


def test_append_agenda_items_tool_caps_one_bump_per_item_per_run(
    db_session, make_pair
):
    """REAL BUG FOUND (confirmed live via agenda_item_history): a single
    run bumped one item's surfaced_count twice within a few seconds — the
    model tool-called the same existing_item_id more than once in one
    meeting, despite the prompt's "one transcript point = one tool entry"
    rule. dedup_guard (app.agenda.store.append_agenda_item) caps this at
    one bump per item per synthesize_tools() run, regardless of how many
    batch entries (or separate tool calls within the run) resolve to it."""
    pair, pair_scope, owner_scope = _scopes(db_session, make_pair)
    tools = synthesize_tools(pair_scope, owner_scope, FrozenClock(at=NOW))
    append_agenda_items_tool = tools[2]

    created = append_agenda_items_tool(
        items=[
            AgendaItemInput(
                text="still working on the migration, no change",
                source="meeting_synthesis",
                created_by_role="report",
                visibility="shared",
            )
        ]
    )[0]
    assert created["surfaced_count"] == 0

    # Same point, restated twice in the SAME meeting's batch — must still
    # land as exactly ONE bump, not two.
    bumped = append_agenda_items_tool(
        items=[
            AgendaItemInput(
                text="still working on the migration, no change",
                source="meeting_synthesis",
                existing_item_id=created["id"],
                created_by_role="report",
                visibility="shared",
            ),
            AgendaItemInput(
                text="still working on the migration, no change",
                source="meeting_synthesis",
                existing_item_id=created["id"],
                created_by_role="report",
                visibility="shared",
            ),
        ]
    )

    assert bumped[0]["surfaced_count"] == 1
    assert bumped[1]["surfaced_count"] == 1


def test_resolve_item_tool_marks_an_agenda_item_resolved(db_session, make_pair):
    pair, pair_scope, owner_scope = _scopes(db_session, make_pair)
    clock = FrozenClock(at=NOW)
    item = append_agenda_item(
        pair_scope,
        {
            "text": "send the deck by Friday",
            "source": "meeting_synthesis",
            "source_link": None,
            "visibility": "shared",
            "created_by_user_id": pair.report_user_id,
            "created_by_role": "report",
        },
        clock,
    )
    tools = synthesize_tools(pair_scope, owner_scope, clock)
    resolve_item_tool = tools[3]

    result = resolve_item_tool(item.id, "report")

    assert result["status"] == "resolved"
    from app.agenda.models import AgendaItem

    row = db_session.get(AgendaItem, item.id)
    assert row.status == "resolved"


def test_append_ledger_item_tool_resolves_created_by_user_id_from_role_not_the_llm(
    db_session, make_pair
):
    """Live-LLM finding: created_by_user_id used to be an LLM-supplied
    tool parameter. Given a real transcript, the model had no legitimate
    way to know either party's real internal user id and filled the
    field with the plain first name it heard ("bob") — a foreign key
    violation against users.id crashed the entire run_post_meeting_flow
    call. created_by_role is now the only signal the tool accepts, and
    the real id is resolved server-side for both roles."""
    pair, pair_scope, owner_scope = _scopes(db_session, make_pair)
    tools = synthesize_tools(pair_scope, owner_scope, FrozenClock(at=NOW))
    append_ledger_items_tool = tools[1]

    report_result = append_ledger_items_tool(items=[
        LedgerItemInput(kind="accomplishment", description="shipped X", created_by_role="report")
    ])[0]
    manager_result = append_ledger_items_tool(items=[
        LedgerItemInput(kind="accomplishment", description="shipped Y", created_by_role="manager")
    ])[0]

    from app.agenda.models import AgendaItem

    report_item = db_session.get(AgendaItem, report_result["id"])
    manager_item = db_session.get(AgendaItem, manager_result["id"])
    assert report_item.created_by_user_id == pair.report_user_id
    assert manager_item.created_by_user_id == pair.manager_user_id


def test_append_ledger_item_tool_commitment_writes_ledger_and_mirrors_agenda(
    db_session, make_pair
):
    pair, pair_scope, owner_scope = _scopes(db_session, make_pair)
    tools = synthesize_tools(pair_scope, owner_scope, FrozenClock(at=NOW))
    append_ledger_items_tool = tools[1]

    result = append_ledger_items_tool(items=[
        LedgerItemInput(
            kind="commitment",
            description="send the deck by Friday",
            created_by_role="report",
            promised_to_person_id=pair.manager_user_id,
            due_at="2026-08-21T00:00:00+00:00",
        )
    ])[0]

    assert result["source"] == "commitment_ledger"
    assert result["text"] == "send the deck by Friday"

    from app.core.models import Commitment

    row = db_session.get(Commitment, result["source_link"])
    assert row is not None
    assert row.description == "send the deck by Friday"
    assert row.promised_to_person_id == pair.manager_user_id


def test_append_ledger_item_tool_commitment_survives_a_non_iso_due_at(
    db_session, make_pair
):
    """Live-LLM finding: a real transcript containing "by Thursday" led
    the synthesize model to pass due_at="Thursday" (not ISO) straight
    through — this crashed the entire run_post_meeting_flow call with an
    uncaught DB error, losing every other item extracted in that same
    turn. due_at is documented as best-effort/optional; an unparseable
    value must be stored as None, not abort the whole synthesis."""
    _pair, pair_scope, owner_scope = _scopes(db_session, make_pair)
    tools = synthesize_tools(pair_scope, owner_scope, FrozenClock(at=NOW))
    append_ledger_items_tool = tools[1]

    result = append_ledger_items_tool(items=[
        LedgerItemInput(
            kind="commitment",
            description="finalize migration tests",
            created_by_role="report",
            due_at="Thursday",
        )
    ])[0]

    from app.core.models import Commitment

    row = db_session.get(Commitment, result["source_link"])
    assert row is not None
    assert row.due_at is None


def test_append_ledger_items_tool_batches_multiple_items_in_one_call(
    db_session, make_pair
):
    """The actual point of this batched tool: multiple commitments/
    accomplishments from the same transcript go in ONE call, not one call
    per item — found live: calling this tool once per item cost one full
    extra model round trip per item (a real, measured ~95-104s end to end
    for a typical 2-item transcript). This confirms one call with several
    entries writes all of them, in order, each to its own correct ledger."""
    pair, pair_scope, owner_scope = _scopes(db_session, make_pair)
    tools = synthesize_tools(pair_scope, owner_scope, FrozenClock(at=NOW))
    append_ledger_items_tool = tools[1]

    results = append_ledger_items_tool(items=[
        LedgerItemInput(
            kind="commitment",
            description="send the deck by Friday",
            created_by_role="report",
            promised_to_person_id=pair.manager_user_id,
        ),
        LedgerItemInput(
            kind="accomplishment",
            description="shipped the migration",
            created_by_role="report",
        ),
        LedgerItemInput(
            kind="commitment",
            description="write the runbook",
            created_by_role="manager",
        ),
    ])

    assert len(results) == 3
    assert [r["source"] for r in results] == [
        "commitment_ledger",
        "accomplishment_ledger",
        "commitment_ledger",
    ]

    from app.core.models import Commitment

    commitment_ids = {results[0]["source_link"], results[2]["source_link"]}
    assert len(commitment_ids) == 2  # two distinct Commitment rows, not one reused twice
    for cid in commitment_ids:
        assert db_session.get(Commitment, cid) is not None


def test_append_ledger_item_tool_accomplishment_writes_ledger_and_mirrors_agenda(
    db_session, make_pair
):
    _pair, pair_scope, owner_scope = _scopes(db_session, make_pair)
    tools = synthesize_tools(pair_scope, owner_scope, FrozenClock(at=NOW))
    append_ledger_items_tool = tools[1]

    result = append_ledger_items_tool(items=[
        LedgerItemInput(
            kind="accomplishment",
            description="shipped the migration",
            created_by_role="report",
        )
    ])[0]

    assert result["source"] == "accomplishment_ledger"

    from app.agenda.models import Accomplishment

    row = db_session.get(Accomplishment, result["source_link"])
    assert row is not None
    assert row.description == "shipped the migration"
    # no explicit occurred_at was passed -- the closure must default it
    # from the clock rather than leaving it unset.
    assert row.occurred_at == NOW


def test_append_ledger_item_tool_accomplishment_links_a_real_goal(db_session, make_pair):
    from app.agenda.models import Accomplishment
    from app.core.models import Goal

    _pair, pair_scope, owner_scope = _scopes(db_session, make_pair)
    goal_id = str(uuid.uuid4())
    db_session.add(
        Goal(
            id=goal_id,
            owner_user_id=pair_scope.report_user_id,
            title="Ship L2 sync",
            status="active",
            created_at=NOW,
            goal_type="key_result",
            source="notion",
            external_id="kr-ext-1",
        )
    )
    db_session.commit()
    tools = synthesize_tools(pair_scope, owner_scope, FrozenClock(at=NOW))
    append_ledger_items_tool = tools[1]

    result = append_ledger_items_tool(items=[
        LedgerItemInput(
            kind="accomplishment",
            description="shipped the L2 sync component",
            created_by_role="report",
            related_key_result_external_id="kr-ext-1",
        )
    ])[0]

    row = db_session.get(Accomplishment, result["source_link"])
    assert row.goal_id == goal_id


def test_append_ledger_item_tool_ignores_a_bogus_related_key_result_id(
    db_session, make_pair
):
    """A hallucinated or stale external_id must never crash the write —
    it just resolves to no link, same "trust nothing from the model
    unverified" posture as append_ledger_item's own source_link handling."""
    from app.agenda.models import Accomplishment

    _pair, pair_scope, owner_scope = _scopes(db_session, make_pair)
    tools = synthesize_tools(pair_scope, owner_scope, FrozenClock(at=NOW))
    append_ledger_items_tool = tools[1]

    result = append_ledger_items_tool(items=[
        LedgerItemInput(
            kind="accomplishment",
            description="shipped something",
            created_by_role="report",
            related_key_result_external_id="does-not-exist",
        )
    ])[0]

    row = db_session.get(Accomplishment, result["source_link"])
    assert row.goal_id is None


def test_append_ledger_item_tool_commitment_ignores_related_key_result_id(
    db_session, make_pair
):
    """related_key_result_external_id only ever applies to accomplishments
    — passing it alongside kind="commitment" must not raise or leak a
    goal_id onto a table that doesn't have one."""
    _pair, pair_scope, owner_scope = _scopes(db_session, make_pair)
    tools = synthesize_tools(pair_scope, owner_scope, FrozenClock(at=NOW))
    append_ledger_items_tool = tools[1]

    result = append_ledger_items_tool(items=[
        LedgerItemInput(
            kind="commitment",
            description="send the deck",
            created_by_role="report",
            related_key_result_external_id="kr-ext-1",
        )
    ])[0]

    assert result["source"] == "commitment_ledger"


def test_prepare_related_key_results_defaults_to_empty_with_no_map_okrs_result(
    db_session, make_pair
):
    _pair, _pair_scope, owner_scope = _scopes(db_session, make_pair)
    callback = _prepare_related_key_results(owner_scope)
    state = {}

    callback(type("Ctx", (), {"state": state})())

    assert state["related_key_results_json"] == "[]"


def test_prepare_related_key_results_never_raises_and_still_sets_fallback(
    db_session, make_pair
):
    """The exact live bug this hardening fixes: a failure anywhere in this
    callback (a malformed map_okrs_result, a DB error, anything) must
    never propagate — ADK's own instruction template raises KeyError for
    a referenced-but-unset state key at render time, which previously
    took down agenda_synthesize's ENTIRE turn before it could call
    append_ledger_item_tool even once, silently dropping both ledgers,
    not just the goal link. Simulated here with a map_okrs_result shaped
    nothing like the real MapOkrsOutput (a plain string, not a dict) —
    .get() on it raises AttributeError, standing in for some real-world
    shape this code didn't anticipate."""
    _pair, _pair_scope, owner_scope = _scopes(db_session, make_pair)
    callback = _prepare_related_key_results(owner_scope)
    state = {"map_okrs_result": "not-a-dict"}

    callback(type("Ctx", (), {"state": state})())

    assert state["related_key_results_json"] == "[]"


def test_prepare_related_key_results_resolves_mapped_and_created_goals(
    db_session, make_pair
):
    import json

    from app.core.models import Goal

    _pair, pair_scope, owner_scope = _scopes(db_session, make_pair)
    db_session.add_all(
        [
            Goal(
                id=str(uuid.uuid4()), owner_user_id=pair_scope.report_user_id,
                title="Mapped KR", status="active", created_at=NOW,
                goal_type="key_result", source="notion", external_id="kr-mapped",
            ),
            Goal(
                id=str(uuid.uuid4()), owner_user_id=pair_scope.report_user_id,
                title="Created KR", status="active", created_at=NOW,
                goal_type="key_result", source="notion", external_id="kr-created",
            ),
            # Not touched this meeting — must not appear.
            Goal(
                id=str(uuid.uuid4()), owner_user_id=pair_scope.report_user_id,
                title="Unrelated KR", status="active", created_at=NOW,
                goal_type="key_result", source="notion", external_id="kr-unrelated",
            ),
        ]
    )
    db_session.commit()
    callback = _prepare_related_key_results(owner_scope)
    state = {
        "map_okrs_result": {
            "mapped_key_results": ["kr-mapped"],
            "created_key_results": ["kr-created"],
            "note_appended": False,
        }
    }

    callback(type("Ctx", (), {"state": state})())

    titles = {g["title"] for g in json.loads(state["related_key_results_json"])}
    assert titles == {"Mapped KR", "Created KR"}


def test_append_ledger_item_tool_dedups_by_source_link_instead_of_duplicating(
    db_session, make_pair
):
    """agenda_synthesize.v1's prompt instructs the LLM to call
    append_ledger_item with the SAME source_link as an existing item to
    dedup instead of creating a duplicate — confirms the tool can
    actually honor that instruction (composition-review finding).

    Uses two SEPARATE synthesize_tools() calls (two separate dedup_guard
    sets), not one tools object called twice: in production, one
    synthesize_tools() call is one meeting run (build_synthesize_agent is
    built fresh per run_post_meeting_flow call), and a real re-mention
    always comes from a genuinely later run — this mirrors that, rather
    than colliding with dedup_guard's own within-one-run cap (see that
    guard's docstring in app.agenda.store.append_agenda_item)."""
    _pair, pair_scope, owner_scope = _scopes(db_session, make_pair)
    description = f"send the deck by Friday [{uuid.uuid4().hex[:8]}]"

    first_tools = synthesize_tools(pair_scope, owner_scope, FrozenClock(at=NOW))
    first = first_tools[1](items=[
        LedgerItemInput(
            kind="commitment",
            description=description,
            created_by_role="report",
        )
    ])[0]

    second_tools = synthesize_tools(pair_scope, owner_scope, FrozenClock(at=NOW))
    second = second_tools[1](items=[
        LedgerItemInput(
            kind="commitment",
            description=description,
            created_by_role="report",
            source_link=first["source_link"],
        )
    ])[0]

    assert first["id"] == second["id"]
    assert second["surfaced_count"] == 1

    from app.core.models import Commitment

    # no second Commitment row was minted for the dedup call
    rows = (
        db_session.query(Commitment).filter(Commitment.description == description).all()
    )
    assert len(rows) == 1


def test_append_ledger_item_tool_ignores_a_bogus_source_link_and_still_writes_ledger(
    db_session, make_pair
):
    """Re-review finding: a wrong/stale/hallucinated source_link must not
    be trusted blindly — that would skip the durable Commitment/
    Accomplishment write while still creating an agenda mirror, silently
    orphaning it from the ledger."""
    _pair, pair_scope, owner_scope = _scopes(db_session, make_pair)
    tools = synthesize_tools(pair_scope, owner_scope, FrozenClock(at=NOW))
    append_ledger_items_tool = tools[1]
    description = f"totally new commitment [{uuid.uuid4().hex[:8]}]"

    result = append_ledger_items_tool(items=[
        LedgerItemInput(
            kind="commitment",
            description=description,
            created_by_role="report",
            source_link="does-not-exist-anywhere",
        )
    ])[0]

    from app.core.models import Commitment

    row = db_session.get(Commitment, result["source_link"])
    assert row is not None
    assert row.description == description


def test_append_agenda_item_tool_direct_item_not_routed_through_ledger(
    db_session, make_pair
):
    _pair, pair_scope, owner_scope = _scopes(db_session, make_pair)
    tools = synthesize_tools(pair_scope, owner_scope, FrozenClock(at=NOW))
    append_agenda_item_tool = tools[2]

    result = append_agenda_item_tool(
        text="revisit onboarding doc next time",
        source="meeting_synthesis",
        source_link=None,
        visibility="shared",
        created_by_role="manager",
    )

    assert result["source"] == "meeting_synthesis"


def test_synthesize_output_round_trips_all_three_lists():
    output = SynthesizeOutput(
        decisions=["decided X"], commitments=["will send Y"], focus_points=["watch Z"]
    )
    assert output.decisions == ["decided X"]
    assert output.commitments == ["will send Y"]
    assert output.focus_points == ["watch Z"]


def test_synthesize_output_requires_all_three_lists():
    # decisions/commitments/focus_points carry no defaults, so omitting
    # any one of them must actually raise -- not just "happen to always
    # be passed" by every caller in this codebase.
    with pytest.raises(pydantic.ValidationError):
        SynthesizeOutput(decisions=["decided X"], commitments=["will send Y"])


def test_build_synthesize_agent_is_wired_with_three_scoped_tools(db_session, make_pair):
    _pair, pair_scope, owner_scope = _scopes(db_session, make_pair)

    agent = build_synthesize_agent(pair_scope, owner_scope, FrozenClock(at=NOW))

    assert agent.name == "agenda_synthesize"
    assert len(agent.tools) == 3
    assert agent.output_schema is SynthesizeOutput
    assert agent.output_key == "synthesize_result"


def test_build_synthesize_agent_runs_at_temperature_zero(db_session, make_pair):
    _pair, pair_scope, owner_scope = _scopes(db_session, make_pair)

    agent = build_synthesize_agent(pair_scope, owner_scope, FrozenClock(at=NOW))

    assert isinstance(agent.generate_content_config, genai_types.GenerateContentConfig)
    assert agent.generate_content_config.temperature == 0


def test_build_synthesize_agent_returns_distinct_instances_per_call(
    db_session, make_pair
):
    # Task 16 calls build_synthesize_agent per meeting -- it must not be a
    # module-level singleton whose tools close over stale scopes.
    _pair, pair_scope, owner_scope = _scopes(db_session, make_pair)

    first = build_synthesize_agent(pair_scope, owner_scope, FrozenClock(at=NOW))
    second = build_synthesize_agent(pair_scope, owner_scope, FrozenClock(at=NOW))

    assert first is not second
    assert first.tools[0] is not second.tools[0]


def test_prompt_id_is_stable():
    assert PROMPT_ID == "agenda_synthesize.v1"
