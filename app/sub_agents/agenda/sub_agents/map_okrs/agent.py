"""ADK MapOkrsAgent — inserted into the post-meeting SequentialAgent right
after Capture, before Synthesize (app/sub_agents/agenda/agent.py). Maps
what Capture found in the transcript onto the mentee's real Notion Key
Results: updates progress on an existing one, creates a new one linked to
the right Objective when nothing fits, and logs a note to the private
"1:1 Notes" db. Follows build_synthesize_agent's exact shape (Agent,
per-invocation tool factory closing over scope) — see that module's own
docstring for why tools can't take scope as a declared LLM-visible
parameter.

Real Notion write shapes (API-post-page/API-patch-page) confirmed live
this session: API-post-page's parent needs the STABLE database_id
(resolve_notion_database_id), NOT the data_source_id API-query-data-source
uses (resolve_notion_data_source_id) — confirmed live these are genuinely
different values and using the wrong one 404s with object_not_found."""

import json
import os
from pathlib import Path

from google.adk.agents import Agent
from google.genai import types as genai_types
from pydantic import BaseModel, Field

from app.agenda.scope import PairScope
from app.core.clock import Clock
from app.core.models import Goal, OneOnOneNote
from app.core.scope import OwnerScope
from app.ingest.live_source import resolve_notion_database_id
from app.ingest.normalize import normalize_goal, normalize_one_on_one_note
from app.tools.mcp_config import McpSession, call_tool, notion_mcp_spec

MODEL = "gemini-3.5-flash-lite"
PROMPT_ID = "agenda_map_okrs.v1"
_PROMPT_PATH = Path(__file__).resolve().parents[4] / "prompts" / "agenda_map_okrs.v1.md"

_KEY_RESULTS_TITLE = "Key Results"
_ONE_ON_ONE_NOTES_TITLE = "1:1 Notes"


class KeyResultActionInput(BaseModel):
    """One piece of concrete progress to record — see
    record_key_result_progress_tool's own docstring for why this is a
    list-typed tool param (batch, don't call once per Key Result). Mirrors
    LedgerItemInput's shape one directory over (app/sub_agents/agenda/
    sub_agents/synthesize/agent.py) — same fix for the same measured
    problem: REAL BUG FOUND LIVE, this agent's prompt used to say "for
    each piece of progress, call update/create_key_result_progress_tool"
    with no batching instruction, exactly the anti-pattern synthesize's
    own tool already had before that fix — a real transcript with several
    Key Results touched drove this single agent's call count past 40,
    each one a real Gemini round trip PLUS a real Notion network call."""

    action: str = Field(description='"update" (existing Key Result) or "create" (new one).')
    key_result_external_id: str | None = Field(
        default=None,
        description="For action=update — the existing Key Result's external_id, from list_key_results_tool. Never invent this.",
    )
    new_current_value: float | None = Field(
        default=None, description="For action=update — the new Current Value."
    )
    objective_external_id: str | None = Field(
        default=None,
        description="For action=create — the parent Objective's external_id, from list_key_results_tool. Never invent this.",
    )
    name: str | None = Field(default=None, description="For action=create — a short name for the new Key Result.")
    target_value: float | None = Field(
        default=None, description="For action=create — what \"done\" looks like, numerically."
    )
    start_value: float = Field(default=0, description="For action=create — starting value, defaults to 0.")


def _goal_to_dict(goal: Goal) -> dict:
    return {
        "external_id": goal.external_id,
        "title": goal.title,
        "goal_type": goal.goal_type,
        "status": goal.status,
        "current_value": goal.current_value,
        "target_value": goal.target_value,
        "progress": goal.progress,
        "parent_external_id": goal.parent_external_id,
        "parent_title": None,
    }


def map_okrs_tools(pair_scope: PairScope, owner_scope: OwnerScope, clock: Clock):
    """Closes over scope, same pattern as synthesize_tools. notion_token
    is read directly from the environment (os.environ via
    notion_mcp_spec), matching every other Notion-touching code in this
    codebase (LiveNotionGoalsClient etc.) — not a Settings field, per
    that existing convention (credentials live in .env, not Settings)."""

    def list_key_results_tool() -> list[dict]:
        """Existing Objectives and Key Results already ingested for this
        mentee (via seed_live/LiveNotionGoalsClient — reads THIS APP'S
        DB, not a live Notion call, so this is cheap and deterministic).
        Call this FIRST, before deciding to update or create anything —
        the model must never invent a Key Result or Objective id; every
        external_id used by the other tools below must come from here.

        Returns:
            A list of goal dicts (external_id, title, goal_type
            "objective" or "key_result", status, current_value,
            target_value, progress, parent_external_id — a key_result's
            parent_external_id is its Objective's external_id).
        """
        rows = (
            owner_scope.session.execute(
                owner_scope.query(Goal).where(
                    Goal.goal_type.in_(["objective", "key_result"])
                )
            )
            .scalars()
            .all()
        )
        objectives_by_external_id = {
            row.external_id: row.title
            for row in rows
            if row.goal_type == "objective"
        }
        result = []
        for row in rows:
            payload = _goal_to_dict(row)
            payload["parent_title"] = (
                objectives_by_external_id.get(row.parent_external_id)
                if row.parent_external_id
                else None
            )
            result.append(payload)
        return result

    def _update_one_key_result(entry: KeyResultActionInput) -> dict:
        """Updates an EXISTING Key Result's progress. Writes to the real
        Notion Key Results db first (source of truth), then mirrors the
        value into this app's own copy so it doesn't drift.

        REAL BUG FOUND AND FIXED (confirmed live): this call's result was
        previously never inspected — API-patch-page against a page that
        is in Notion's trash (or otherwise rejected, e.g. a real 400) does
        NOT raise (confirmed live: Notion's MCP wrapper returns the error
        as normal tool content, {"object": "error", ...}, not an
        MCP-protocol-level error — call_tool only raises on the latter),
        so the local Goal mirror below was unconditionally updated to the
        new value regardless of whether Notion actually accepted the
        write. That silently diverged this app's own "source of truth"
        copy from the real Notion page it claims to mirror — the local DB
        showed a Key Result as updated while the real Notion page stayed
        untouched (and, in the incident that surfaced this, stayed
        trashed). Same defensive shape _create_one_key_result already
        uses a few lines below: check the write actually succeeded before
        touching anything local."""
        key_result_external_id = entry.key_result_external_id
        new_current_value = entry.new_current_value
        result = call_tool(
            notion_mcp_spec(_notion_token()),
            "API-patch-page",
            {
                "page_id": key_result_external_id,
                "properties": {"Current Value": {"number": new_current_value}},
            },
        )
        if not isinstance(result, dict) or result.get("object") != "page":
            error_message = (
                result.get("message")
                if isinstance(result, dict)
                else f"unexpected API-patch-page response: {result!r}"
            )
            return {
                "external_id": key_result_external_id,
                "status": "notion_write_failed",
                "error": error_message or "Notion rejected the write",
            }
        existing = owner_scope.session.execute(
            owner_scope.query(Goal).where(
                Goal.source == "notion", Goal.external_id == key_result_external_id
            )
        ).scalar_one_or_none()
        if existing is not None:
            existing.current_value = new_current_value
            # Stamped here too, not just normalize_goal's upsert path —
            # this call bypasses normalize_goal entirely (direct mutation
            # above). See Goal.updated_at's own docstring: the live
            # dashboard's SSE relay needs this to notice a KR's progress
            # moved during a meeting, in real time.
            existing.updated_at = clock.now()
            owner_scope.session.commit()
            return _goal_to_dict(existing)
        return {"external_id": key_result_external_id, "current_value": new_current_value}

    def _create_one_key_result(entry: KeyResultActionInput) -> dict:
        """Creates a NEW Key Result — use this only when nothing returned
        by list_key_results_tool fits what the transcript described.
        Creates the real page in Notion's Key Results db, linked to the
        given Objective, then mirrors it into this app's own Goal table
        so it's visible immediately without waiting for the next
        seed_live poll."""
        objective_external_id = entry.objective_external_id
        name = entry.name
        target_value = entry.target_value
        start_value = entry.start_value
        with McpSession(notion_mcp_spec(_notion_token())) as session:
            database_id = resolve_notion_database_id(session, _KEY_RESULTS_TITLE)
            if database_id is None:
                raise RuntimeError(
                    f"could not resolve the {_KEY_RESULTS_TITLE!r} database — "
                    "is it still shared with the Notion integration?"
                )
            result = session.call(
                "API-post-page",
                {
                    "parent": {"type": "database_id", "database_id": database_id},
                    "properties": {
                        "Key Result Name": {"title": [{"text": {"content": name}}]},
                        "Target Value": {"number": target_value},
                        "Start Value": {"number": start_value},
                        "Objective": {"relation": [{"id": objective_external_id}]},
                    },
                },
            )
        new_page_id = result["id"] if isinstance(result, dict) else None
        if not new_page_id:
            raise RuntimeError(f"API-post-page did not return a page id: {result!r}")

        row = normalize_goal(
            owner_scope,
            {
                "source": "notion",
                "external_id": new_page_id,
                "goal_type": "key_result",
                "title": name,
                "target_value": target_value,
                "current_value": start_value,
                "parent_external_id": objective_external_id,
            },
            clock,
        )
        return _goal_to_dict(row)

    def record_key_result_progress_tool(entries: list[KeyResultActionInput]) -> list[dict]:
        """Records one or more pieces of concrete Key Result progress —
        updates to existing Key Results and/or brand-new ones — in a
        single call.

        PASS EVERY piece of concrete progress from this transcript in ONE
        call, not one call per Key Result — same reasoning and the same
        measured cost as append_ledger_items_tool's own docstring (app/
        sub_agents/agenda/sub_agents/synthesize/agent.py): each call you
        make is a full extra Gemini round trip, and here each entry ALSO
        does a real synchronous Notion API write inside it — a transcript
        that touches 5 Key Results one-call-at-a-time was 5+ extra round
        trips beyond what batching costs, found live driving a single
        meeting's total call count past 40.

        Args:
            entries: One KeyResultActionInput per piece of progress —
                action ("update" or "create") plus whichever of that
                action's fields apply (see KeyResultActionInput's own
                field descriptions). Never invent an external_id; only
                use ones returned by list_key_results_tool.

        Returns:
            One resulting Key Result dict per input entry, same order.
            An update entry whose real Notion write failed comes back as
            {"status": "notion_write_failed", "error": ...} instead of a
            Key Result dict — see _update_one_key_result's own docstring.
        """
        results = []
        for entry in entries:
            if entry.action == "update":
                results.append(_update_one_key_result(entry))
            elif entry.action == "create":
                results.append(_create_one_key_result(entry))
            else:
                results.append(
                    {"status": "invalid_action", "error": f"unknown action {entry.action!r}"}
                )
        return results

    def append_one_on_one_note_tool(
        summary: str, linked_key_result_external_ids: list[str]
    ) -> dict:
        """Logs a private note about this meeting to the mentee's "1:1
        Notes" Notion db, linked to whichever Key Results this meeting's
        content mapped to (updated or newly created). Manager visibility
        into this comes from Notion's own sharing of the Career Goals/
        Key Results dbs — this app does not separately gate visibility,
        since it does not own access control for Notion content.

        Args:
            summary: A short summary of what was discussed, safe to
                store as the note's title.
            linked_key_result_external_ids: external_id(s) of the Key
                Results this meeting's content relates to — from
                list_key_results_tool, or a newly created one's
                external_id from create_key_result_tool.

        Returns:
            The newly created 1:1 Note as a dict.
        """
        with McpSession(notion_mcp_spec(_notion_token())) as session:
            database_id = resolve_notion_database_id(session, _ONE_ON_ONE_NOTES_TITLE)
            if database_id is None:
                raise RuntimeError(
                    f"could not resolve the {_ONE_ON_ONE_NOTES_TITLE!r} database — "
                    "is it still shared with the Notion integration?"
                )
            result = session.call(
                "API-post-page",
                {
                    "parent": {"type": "database_id", "database_id": database_id},
                    "properties": {
                        "Meeting Note": {"title": [{"text": {"content": summary}}]},
                        "Key Results": {
                            "relation": [
                                {"id": kr_id}
                                for kr_id in linked_key_result_external_ids
                            ]
                        },
                    },
                },
            )
        new_page_id = result["id"] if isinstance(result, dict) else None
        if not new_page_id:
            raise RuntimeError(f"API-post-page did not return a page id: {result!r}")

        row = normalize_one_on_one_note(
            owner_scope,
            {
                "source": "notion",
                "external_id": new_page_id,
                "title": summary,
                "linked_key_result_external_ids": json.dumps(
                    linked_key_result_external_ids
                ),
            },
            clock,
        )
        return {
            "external_id": row.external_id,
            "title": row.title,
            "linked_key_result_external_ids": linked_key_result_external_ids,
        }

    return [
        list_key_results_tool,
        record_key_result_progress_tool,
        append_one_on_one_note_tool,
    ]


def _notion_token() -> str | None:
    return os.environ.get("NOTION_TOKEN")


class MapOkrsOutput(BaseModel):
    mapped_key_results: list[str]
    created_key_results: list[str]
    note_appended: bool


def build_map_okrs_agent(
    pair_scope: PairScope, owner_scope: OwnerScope, clock: Clock
) -> Agent:
    """Built per-invocation, not module-level — same reason as
    build_synthesize_agent: its tools close over a specific meeting's
    scopes."""
    return Agent(
        name="agenda_map_okrs",
        model=MODEL,
        instruction=_PROMPT_PATH.read_text(encoding="utf-8"),
        tools=map_okrs_tools(pair_scope, owner_scope, clock),
        output_schema=MapOkrsOutput,
        output_key="map_okrs_result",
        generate_content_config=genai_types.GenerateContentConfig(temperature=0),
    )
