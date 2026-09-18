import datetime
import json
import uuid

from app.agenda.scope import resolve_pair_scope
from app.core.clock import FrozenClock
from app.core.models import Goal
from app.core.scope import OwnerScope
from app.sub_agents.agenda.sub_agents.map_okrs.agent import (
    PROMPT_ID,
    KeyResultActionInput,
    map_okrs_tools,
)

NOW = datetime.datetime(2026, 8, 17, tzinfo=datetime.UTC)


def _scopes(db_session, make_pair):
    pair = make_pair()
    pair_scope = resolve_pair_scope(
        db_session, pair.report_user_id, pair.report_user_id
    )
    owner_scope = OwnerScope(owner_user_id=pair.report_user_id, session=db_session)
    return pair, pair_scope, owner_scope


def _seed_goal(owner_scope, **overrides) -> Goal:
    defaults = dict(
        id=str(uuid.uuid4()),
        title="Objective",
        status="active",
        created_at=NOW,
        source="notion",
        goal_type="objective",
        external_id="obj-1",
    )
    defaults.update(overrides)
    goal = Goal(**defaults)
    owner_scope.add(goal)
    owner_scope.session.commit()
    return goal


class _FakeMcpSession:
    def __init__(self, spec, fake_call_tool):
        self._spec = spec
        self._fake_call_tool = fake_call_tool

    def __enter__(self):
        return self

    def call(self, tool_name, arguments, timeout=None):
        return self._fake_call_tool(self._spec, tool_name, arguments)

    def __exit__(self, exc_type, exc, tb):
        return False


def _fake_mcp_session(fake_call_tool):
    return lambda spec: _FakeMcpSession(spec, fake_call_tool)


def _search_result(title: str, ds_id: str, db_id: str) -> dict:
    return {
        "results": [
            {
                "object": "data_source",
                "id": ds_id,
                "title": [{"plain_text": title}],
                "parent": {"type": "database_id", "database_id": db_id},
            }
        ]
    }


def test_list_key_results_tool_returns_objectives_and_key_results(
    db_session, make_pair
):
    pair, pair_scope, owner_scope = _scopes(db_session, make_pair)
    _seed_goal(owner_scope, external_id="obj-1", goal_type="objective", title="Grow")
    _seed_goal(
        owner_scope,
        external_id="kr-1",
        goal_type="key_result",
        title="Ship 2 features",
        parent_external_id="obj-1",
    )
    # a career_goal must NOT show up — only objective/key_result are
    # relevant to what this agent maps transcripts onto.
    _seed_goal(owner_scope, external_id="cg-1", goal_type="career_goal")

    tools = map_okrs_tools(pair_scope, owner_scope, FrozenClock(at=NOW))
    list_key_results_tool = tools[0]

    result = list_key_results_tool()

    external_ids = {r["external_id"] for r in result}
    assert external_ids == {"obj-1", "kr-1"}
    kr_row = next(r for r in result if r["external_id"] == "kr-1")
    assert kr_row["parent_title"] == "Grow"


def test_update_key_result_progress_tool_writes_notion_and_local_row(
    db_session, make_pair, monkeypatch
):
    pair, pair_scope, owner_scope = _scopes(db_session, make_pair)
    _seed_goal(
        owner_scope,
        external_id="kr-1",
        goal_type="key_result",
        current_value=1,
    )
    calls = []

    def fake_call_tool(spec, tool_name, arguments):
        calls.append((tool_name, arguments))
        # Real API-patch-page success shape (confirmed live): {"object":
        # "page", "id": ..., "properties": {...}}.
        return {"object": "page", "id": "kr-1"}

    monkeypatch.setattr(
        "app.sub_agents.agenda.sub_agents.map_okrs.agent.call_tool", fake_call_tool
    )
    monkeypatch.setenv("NOTION_TOKEN", "fake-token")

    tools = map_okrs_tools(pair_scope, owner_scope, FrozenClock(at=NOW))
    record_key_result_progress_tool = tools[1]

    result = record_key_result_progress_tool(
        entries=[
            KeyResultActionInput(
                action="update", key_result_external_id="kr-1", new_current_value=2
            )
        ]
    )[0]

    assert result["current_value"] == 2
    assert calls[0][0] == "API-patch-page"
    assert calls[0][1]["page_id"] == "kr-1"
    assert calls[0][1]["properties"]["Current Value"]["number"] == 2

    row = (
        db_session.query(Goal)
        .filter(Goal.owner_user_id == pair.report_user_id, Goal.external_id == "kr-1")
        .one()
    )
    assert row.current_value == 2


def test_update_key_result_progress_tool_does_not_touch_local_row_on_notion_failure(
    db_session, make_pair, monkeypatch
):
    """Regression test for a real incident: API-patch-page against a page
    that is in Notion's trash (or otherwise rejected) does NOT raise —
    Notion's MCP wrapper returns the error as normal tool content
    ({"object": "error", ...}), not an MCP-protocol-level error. The old
    code never inspected the result and always updated the local Goal
    mirror, so the local DB showed a Key Result as updated while the
    real Notion page stayed untouched (and, in the real incident this
    responds to, stayed trashed)."""
    pair, pair_scope, owner_scope = _scopes(db_session, make_pair)
    _seed_goal(
        owner_scope,
        external_id="kr-1",
        goal_type="key_result",
        current_value=45,
    )

    def fake_call_tool(spec, tool_name, arguments):
        return {
            "object": "error",
            "status": 400,
            "code": "validation_error",
            "message": "Page is in trash.",
        }

    monkeypatch.setattr(
        "app.sub_agents.agenda.sub_agents.map_okrs.agent.call_tool", fake_call_tool
    )
    monkeypatch.setenv("NOTION_TOKEN", "fake-token")

    tools = map_okrs_tools(pair_scope, owner_scope, FrozenClock(at=NOW))
    record_key_result_progress_tool = tools[1]

    result = record_key_result_progress_tool(
        entries=[
            KeyResultActionInput(
                action="update", key_result_external_id="kr-1", new_current_value=20
            )
        ]
    )[0]

    assert result["status"] == "notion_write_failed"
    assert "trash" in result["error"].lower()

    row = (
        db_session.query(Goal)
        .filter(Goal.owner_user_id == pair.report_user_id, Goal.external_id == "kr-1")
        .one()
    )
    assert row.current_value == 45


def test_create_key_result_tool_creates_in_notion_and_locally(
    db_session, make_pair, monkeypatch
):
    pair, pair_scope, owner_scope = _scopes(db_session, make_pair)
    _seed_goal(owner_scope, external_id="obj-1", goal_type="objective")

    def fake_call_tool(spec, tool_name, arguments):
        if tool_name == "API-post-search":
            return _search_result("Key Results", "kr-ds", "kr-db")
        assert tool_name == "API-post-page"
        assert arguments["parent"] == {"type": "database_id", "database_id": "kr-db"}
        assert arguments["properties"]["Objective"]["relation"] == [{"id": "obj-1"}]
        return {"id": "new-kr-1"}

    monkeypatch.setattr(
        "app.sub_agents.agenda.sub_agents.map_okrs.agent.McpSession",
        _fake_mcp_session(fake_call_tool),
    )
    monkeypatch.setenv("NOTION_TOKEN", "fake-token")

    tools = map_okrs_tools(pair_scope, owner_scope, FrozenClock(at=NOW))
    record_key_result_progress_tool = tools[1]

    result = record_key_result_progress_tool(
        entries=[
            KeyResultActionInput(
                action="create",
                objective_external_id="obj-1",
                name="New KR",
                target_value=5,
            )
        ]
    )[0]

    assert result["external_id"] == "new-kr-1"
    assert result["goal_type"] == "key_result"
    assert result["parent_external_id"] == "obj-1"

    rows = (
        db_session.query(Goal)
        .filter(Goal.owner_user_id == pair.report_user_id, Goal.external_id == "new-kr-1")
        .all()
    )
    assert len(rows) == 1
    assert rows[0].target_value == 5


def test_append_one_on_one_note_tool_creates_in_notion_and_locally(
    db_session, make_pair, monkeypatch
):
    pair, pair_scope, owner_scope = _scopes(db_session, make_pair)

    def fake_call_tool(spec, tool_name, arguments):
        if tool_name == "API-post-search":
            return _search_result("1:1 Notes", "notes-ds", "notes-db")
        assert tool_name == "API-post-page"
        assert arguments["parent"] == {
            "type": "database_id",
            "database_id": "notes-db",
        }
        assert arguments["properties"]["Key Results"]["relation"] == [
            {"id": "kr-1"}
        ]
        return {"id": "new-note-1"}

    monkeypatch.setattr(
        "app.sub_agents.agenda.sub_agents.map_okrs.agent.McpSession",
        _fake_mcp_session(fake_call_tool),
    )
    monkeypatch.setenv("NOTION_TOKEN", "fake-token")

    tools = map_okrs_tools(pair_scope, owner_scope, FrozenClock(at=NOW))
    append_one_on_one_note_tool = tools[2]

    result = append_one_on_one_note_tool("Discussed progress.", ["kr-1"])

    assert result["external_id"] == "new-note-1"
    assert result["linked_key_result_external_ids"] == ["kr-1"]

    from app.core.models import OneOnOneNote

    row = (
        db_session.query(OneOnOneNote)
        .filter(
            OneOnOneNote.owner_user_id == pair.report_user_id,
            OneOnOneNote.external_id == "new-note-1",
        )
        .one()
    )
    assert row.title == "Discussed progress."
    assert json.loads(row.linked_key_result_external_ids) == ["kr-1"]


def test_record_key_result_progress_tool_batches_update_and_create_in_one_call(
    db_session, make_pair, monkeypatch
):
    """REAL BUG FOUND LIVE: the old one-call-per-Key-Result tools drove a
    single meeting's call count past 40 (each call is a full extra Gemini
    round trip PLUS a real Notion write) — this is the fix, mirroring
    synthesize's own append_ledger_items_tool batching test."""
    pair, pair_scope, owner_scope = _scopes(db_session, make_pair)
    _seed_goal(owner_scope, external_id="obj-1", goal_type="objective")
    _seed_goal(
        owner_scope, external_id="kr-1", goal_type="key_result", current_value=1
    )

    patch_calls = []

    def fake_call_tool(spec, tool_name, arguments):
        patch_calls.append((tool_name, arguments))
        return {"object": "page", "id": "kr-1"}

    def fake_mcp_call(spec, tool_name, arguments):
        if tool_name == "API-post-search":
            return _search_result("Key Results", "kr-ds", "kr-db")
        return {"id": "new-kr-2"}

    monkeypatch.setattr(
        "app.sub_agents.agenda.sub_agents.map_okrs.agent.call_tool", fake_call_tool
    )
    monkeypatch.setattr(
        "app.sub_agents.agenda.sub_agents.map_okrs.agent.McpSession",
        _fake_mcp_session(fake_mcp_call),
    )
    monkeypatch.setenv("NOTION_TOKEN", "fake-token")

    tools = map_okrs_tools(pair_scope, owner_scope, FrozenClock(at=NOW))
    record_key_result_progress_tool = tools[1]

    results = record_key_result_progress_tool(
        entries=[
            KeyResultActionInput(
                action="update", key_result_external_id="kr-1", new_current_value=3
            ),
            KeyResultActionInput(
                action="create",
                objective_external_id="obj-1",
                name="Second KR",
                target_value=10,
            ),
        ]
    )

    assert len(results) == 2
    assert results[0]["current_value"] == 3
    assert results[1]["external_id"] == "new-kr-2"

    updated_row = (
        db_session.query(Goal)
        .filter(Goal.owner_user_id == pair.report_user_id, Goal.external_id == "kr-1")
        .one()
    )
    assert updated_row.current_value == 3
    assert updated_row.updated_at == NOW

    created_row = (
        db_session.query(Goal)
        .filter(
            Goal.owner_user_id == pair.report_user_id, Goal.external_id == "new-kr-2"
        )
        .one()
    )
    assert created_row.target_value == 10


def test_prompt_id_matches_the_committed_prompt_file():
    assert PROMPT_ID == "agenda_map_okrs.v1"
