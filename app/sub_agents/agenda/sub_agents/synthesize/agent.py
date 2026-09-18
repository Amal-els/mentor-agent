"""ADK SynthesizeOutcomeAgent — second step of the post-meeting
SequentialAgent (design spec §4.4b). Reads the agenda first to dedup,
then routes every new item through the same append_ledger_item/
append_agenda_item split component 2 (event-driven ingest) uses — reused,
not duplicated."""

import json
import logging
from pathlib import Path

from google.adk.agents import Agent
from google.genai import types as genai_types
from pydantic import BaseModel, Field

from app.agenda.scope import PairScope
from app.agenda.store import (
    append_agenda_item,
    append_ledger_item,
    get_full_agenda,
    mark_resolved,
)
from app.core.clock import Clock
from app.core.models import Goal
from app.core.scope import OwnerScope

logger = logging.getLogger(__name__)

MODEL = "gemini-3.5-flash-lite"
PROMPT_ID = "agenda_synthesize.v1"
_PROMPT_PATH = (
    Path(__file__).resolve().parents[4] / "prompts" / "agenda_synthesize.v1.md"
)


class LedgerItemInput(BaseModel):
    """One commitment/accomplishment to record — see
    append_ledger_items_tool's own docstring for why this is a list-typed
    tool param (batch, don't call once per item)."""

    kind: str = Field(description='"commitment" or "accomplishment".')
    description: str = Field(description="What was promised or accomplished.")
    created_by_role: str = Field(
        description=(
            'Who is recording this — "manager" or "report", whichever '
            "party said/committed to it in the meeting."
        )
    )
    promised_to_person_id: str | None = Field(
        default=None, description="For commitments — who it's owed to, if known."
    )
    due_at: str | None = Field(
        default=None, description="For commitments — ISO timestamp, if known."
    )
    occurred_at: str | None = Field(
        default=None, description="For accomplishments — ISO timestamp."
    )
    existing_item_id: str | None = Field(
        default=None,
        description=(
            "The id of an existing agenda item returned by get_agenda_tool "
            "that this is a re-mention of. Use this when the transcript is "
            "referring to something already visible in get_agenda_tool's "
            "output."
        ),
    )
    source_link: str | None = Field(
        default=None,
        description=(
            "Pass the SAME source_link as an existing item returned by "
            "get_agenda_tool when this is a re-mention of something "
            "already on the agenda — bumps its surfaced_count instead of "
            "creating a duplicate. Omit when this is genuinely new."
        ),
    )
    related_key_result_external_id: str | None = Field(
        default=None,
        description=(
            "For accomplishments only — the external_id of a Key Result "
            'from "KEY RESULTS THIS MEETING ALREADY TOUCHED" (populated '
            "from MapOkrs) that this accomplishment clearly moved "
            "forward. Never invent one; omit when nothing fits."
        ),
    )


class AgendaItemInput(BaseModel):
    text: str = Field(description="The item's display text.")
    source: str = Field(description='One of "jira", "slack", "meeting_synthesis".')
    existing_item_id: str | None = Field(
        default=None,
        description=(
            "The id of an existing item from get_agenda_tool's output that "
            "this is a re-mention of. Set this whenever the point you're "
            "recording already appears in get_agenda_tool's result."
        ),
    )
    source_link: str | None = Field(
        default=None,
        description=(
            "External reference (Jira ticket, Slack thread) to dedup against, "
            "if any. Omit if existing_item_id is set."
        ),
    )
    visibility: str = Field(description='"shared", "manager_only", or "report_only".')
    created_by_role: str = Field(description='"manager" or "report".')


def _item_to_dict(item) -> dict:
    return {
        "id": item.id,
        "text": item.text,
        "source": item.source,
        "source_link": item.source_link,
        "status": item.status,
        "surfaced_count": item.surfaced_count,
    }


def _agenda_items_docstring() -> str:
    return (
        "Adds or reuses one or more agenda items directly (jira_blocker/"
        "slack_q/meeting_synthesis — never for commitment/accomplishment, "
        "use append_ledger_items_tool for those instead).\n\n"
        "FIRST call get_agenda_tool to see what already exists. THEN read "
        "through the ENTIRE transcript and build the complete list of "
        "distinct points before calling this tool — do not call it "
        "incrementally as you go. PASS EVERY meeting_synthesis/jira/slack "
        "item in ONE call, same as append_ledger_items_tool.\n\n"
        "Each entry must correspond to exactly ONE distinct point from the "
        "transcript. Never combine two unrelated topics into a single item's "
        "text, even if they feel loosely related — two separate things in the "
        "transcript means two separate entries here, never one merged "
        "sentence.\n\n"
        "For every entry, check whether it matches something already returned "
        "by get_agenda_tool. If it does, set that entry's existing_item_id to "
        "the existing item's id. Only leave existing_item_id unset when the "
        "point is genuinely new."
    )


def synthesize_tools(pair_scope: PairScope, owner_scope: OwnerScope, clock: Clock):
    """ADK tools are plain functions the LLM calls with no scope
    parameter of its own (the model only ever sees the tool's declared
    args) — so pair_scope/owner_scope/clock must be closed over here
    rather than threaded through the tool signature. This factory is
    called once per meeting, right before this agent runs, with the real
    scopes for that meeting's pair.

    created_by_user_id is deliberately NOT a tool parameter (a live run
    against a real transcript proved why: the LLM has no legitimate way
    to know either party's real internal user id, so it filled the field
    with the plain first name it heard in the transcript — "bob" — which
    crashed the whole run with a foreign key violation against users.id).
    created_by_role ("report"/"manager") is something the model CAN
    reliably infer from who's speaking, and that's enough to resolve the
    real id deterministically server-side — a 1-on-1 only ever has these
    two parties."""
    from app.agenda.scope import get_current_pair

    pair = get_current_pair(pair_scope.session, pair_scope.report_user_id)
    manager_user_id = pair.manager_user_id if pair is not None else None

    # REAL BUG FOUND (confirmed live via agenda_item_history): the model
    # doesn't reliably collapse every mention of the same point within
    # ONE transcript into a single tool entry, despite the prompt saying
    # to — a real run bumped one item's surfaced_count twice within the
    # same few seconds. Shared across every append_agenda_item/
    # append_ledger_item call this factory's tools make for the rest of
    # this meeting's synthesis turn, so store.py's own dedup_guard check
    # caps each existing item at one bump per run regardless of how many
    # entries in the batch (or how many separate tool calls) resolve to
    # it — never trusting the model's batching discipline on faith.
    dedup_guard: set[str] = set()

    def _resolve_created_by_user_id(created_by_role: str) -> str:
        if created_by_role == "report":
            return pair_scope.report_user_id
        if created_by_role == "manager" and manager_user_id is not None:
            return manager_user_id
        raise ValueError(
            f"cannot resolve created_by_user_id for created_by_role="
            f"{created_by_role!r} (expected 'report' or 'manager', with a "
            "resolvable current Pair for the manager case)"
        )

    def get_agenda_tool() -> list[dict]:
        """Reads the current agenda for this pair. Call this FIRST,
        before recording anything, to dedup against existing items.
        Returns every item regardless of visibility (get_full_agenda, not
        get_agenda) — dedup must not be blind to a report_only/
        manager_only item just because of who triggered this meeting.

        Returns:
            A list of agenda item dicts (id, text, source, source_link,
            status, surfaced_count).
        """
        items = [_item_to_dict(item) for item in get_full_agenda(pair_scope)]
        logger.info(
            "get_agenda_tool: returning %d item(s): %s",
            len(items),
            [(i["id"], i["surfaced_count"], i["text"][:60]) for i in items],
        )
        return items

    def _append_one_agenda_item(entry: AgendaItemInput) -> dict:
        result = append_agenda_item(
            pair_scope,
            {
                "text": entry.text,
                "source": entry.source,
                "existing_item_id": entry.existing_item_id,
                "source_link": entry.source_link,
                "visibility": entry.visibility,
                "created_by_user_id": _resolve_created_by_user_id(entry.created_by_role),
                "created_by_role": entry.created_by_role,
            },
            clock,
            dedup_guard=dedup_guard,
        )
        return _item_to_dict(result)

    def _resolve_related_goal_id(related_key_result_external_id: str | None) -> str | None:
        if not related_key_result_external_id:
            return None
        # Resolved server-side, never trusted as a real Goal.id straight
        # from the model — owner_scope.query already scopes this to the
        # current owner, so a hallucinated or cross-owner id just
        # resolves to nothing (None) rather than linking to someone
        # else's goal.
        goal = owner_scope.session.execute(
            owner_scope.query(Goal).where(
                Goal.external_id == related_key_result_external_id
            )
        ).scalar_one_or_none()
        return goal.id if goal is not None else None

    def _append_one_ledger_item(entry: LedgerItemInput) -> dict:
        item = {
            "description": entry.description,
            "created_by_user_id": _resolve_created_by_user_id(entry.created_by_role),
            "created_by_role": entry.created_by_role,
            "existing_item_id": entry.existing_item_id,
            "source_link": entry.source_link,
        }
        if entry.kind == "commitment":
            item["promised_to_person_id"] = entry.promised_to_person_id
            item["due_at"] = entry.due_at
        else:
            item["occurred_at"] = entry.occurred_at or clock.now().isoformat()
            item["goal_id"] = _resolve_related_goal_id(
                entry.related_key_result_external_id
            )
        result = append_ledger_item(
            pair_scope, owner_scope, entry.kind, item, clock, dedup_guard=dedup_guard
        )
        return _item_to_dict(result)

    def append_ledger_items_tool(items: list[LedgerItemInput]) -> list[dict]:
        """Records one or more commitments/accomplishments to the durable
        ledger and mirrors a pointer onto the agenda for each.

        PASS EVERY commitment/accomplishment from this transcript in ONE
        call — do not call this once per item. Found live: calling this
        tool once per item cost one full extra model round trip per item
        (a real, measured 95-104s end to end for a typical 2-item
        transcript, largely from calling this exact tool twice); batching
        is what actually cuts that down, not model or prompt tuning.

        Args:
            items: One LedgerItemInput per commitment/accomplishment —
                kind ("commitment" or "accomplishment"), description
                (what was promised or accomplished), created_by_role
                ("manager" or "report" — whoever said/committed to it),
                and whichever of the kind-specific optional fields apply
                (see LedgerItemInput's own field descriptions). For every
                entry, check whether it matches something already returned
                by get_agenda_tool and set existing_item_id when it does.

        Returns:
            One resulting agenda item dict per input item, same order.
        """
        # Observability for the "re-mention silently omitted from the
        # batch" failure mode: logs exactly what the model chose to
        # include (and whether it set existing_item_id) BEFORE any store
        # write happens, so a live trace can distinguish "tool wasn't
        # called with this item at all" from "it was called but the
        # store didn't bump it."
        logger.info(
            "append_ledger_items_tool: batch of %d item(s): %s",
            len(items),
            [(i.kind, i.existing_item_id, i.description[:60]) for i in items],
        )
        return [_append_one_ledger_item(entry) for entry in items]

    def append_agenda_items_tool(items: list[AgendaItemInput]) -> list[dict]:
        """Adds or reuses one or more agenda items directly (jira_blocker/
        slack_q/meeting_synthesis — never for commitment/accomplishment,
        use append_ledger_items_tool for those instead).

        FIRST call get_agenda_tool to see what already exists. THEN read
        the entire transcript and build the complete list of distinct
        points before calling this tool — do not call it incrementally as
        you go. PASS EVERY meeting_synthesis/jira/slack item in ONE call,
        same as append_ledger_items_tool.

        Each entry must correspond to exactly ONE distinct point from the
        transcript. Never combine two unrelated topics into a single
        item's text, even loosely related ones — one point, one entry.

        For every entry, check whether it matches something already
        returned by get_agenda_tool. If it does, set that entry's
        existing_item_id to the existing item's id. Only leave
        existing_item_id unset when the point is genuinely new.

        Args:
            items: One AgendaItemInput per distinct point.

        Returns:
            One resulting agenda item dict per input item, same order.
        """
        logger.info(
            "append_agenda_items_tool: batch of %d item(s): %s",
            len(items),
            [(i.source, i.existing_item_id, i.text[:60]) for i in items],
        )
        return [_append_one_agenda_item(entry) for entry in items]

    def resolve_item_tool(item_id: str, resolved_by_role: str) -> dict:
        """Marks an existing agenda item as resolved."""
        result = mark_resolved(
            pair_scope,
            item_id,
            _resolve_created_by_user_id(resolved_by_role),
            clock,
        )
        return _item_to_dict(result)

    return [
        get_agenda_tool,
        append_ledger_items_tool,
        append_agenda_items_tool,
        resolve_item_tool,
    ]


class SynthesizeOutput(BaseModel):
    decisions: list[str]
    commitments: list[str]
    focus_points: list[str]


def _prepare_related_key_results(owner_scope: OwnerScope):
    """before_agent_callback factory — seeds related_key_results_json
    (referenced by agenda_synthesize.v1.md's own {related_key_results_json}
    placeholder) from MapOkrs' output, same "read map_okrs_result off
    shared SequentialAgent session state" mechanism app/sub_agents/pulse/
    sub_agents/writer/agent.py's _prepare_writer_input already uses for
    ranker_result. MapOkrs runs immediately before this agent in
    app/sub_agents/agenda/agent.py's SequentialAgent, so map_okrs_result
    is always already in state by the time this fires — absent only for
    a standalone caller that never ran MapOkrs at all (defaults to "[]",
    same as ranker_agent's _default_previous_critique does for its own
    optional predecessor state).

    Only titles for the specific external_ids MapOkrs actually touched
    this meeting (mapped or newly created) — not the full Key Results
    list list_key_results_tool would return — so the model links an
    accomplishment to something this meeting is already known to have
    moved, never to an arbitrary unrelated OKR it happens to remember
    from earlier in the conversation.

    Every line below is wrapped in try/except: agenda_synthesize.v1.md's
    prompt references {related_key_results_json} with no `?` (non-
    optional) — ADK's own inject_session_state raises KeyError for a
    referenced state key that's missing at render time (confirmed by
    reading google.adk.utils.instructions_utils directly), which would
    abort this agent's entire turn before it ever calls append_ledger_
    item_tool. Found live: exactly that — a real run recorded nothing to
    either ledger at all, not just missing the goal link, because this
    optional enhancement's own failure silently took down core ledger
    logging with it. A goal-linking hint that fails to build must never
    be worse than not having it; "[]" (no linked Key Results this turn)
    is always a safe, valid fallback."""

    def _callback(callback_context) -> None:
        try:
            state = callback_context.state
            map_okrs_result = state.get("map_okrs_result") or {}
            external_ids = list(
                map_okrs_result.get("mapped_key_results", [])
            ) + list(map_okrs_result.get("created_key_results", []))
            if not external_ids:
                state["related_key_results_json"] = "[]"
                return
            rows = owner_scope.session.execute(
                owner_scope.query(Goal).where(Goal.external_id.in_(external_ids))
            ).scalars().all()
            state["related_key_results_json"] = json.dumps(
                [{"external_id": g.external_id, "title": g.title} for g in rows]
            )
        except Exception:
            logger.exception(
                "_prepare_related_key_results: failed to resolve related "
                "Key Results, falling back to none so ledger logging still "
                "runs"
            )
            callback_context.state["related_key_results_json"] = "[]"

    return _callback


def build_synthesize_agent(
    pair_scope: PairScope, owner_scope: OwnerScope, clock: Clock
) -> Agent:
    """Built per-invocation, not module-level, since its tools close over
    a specific meeting's scopes (see synthesize_tools' docstring)."""
    return Agent(
        name="agenda_synthesize",
        before_agent_callback=_prepare_related_key_results(owner_scope),
        model=MODEL,
        instruction=_PROMPT_PATH.read_text(encoding="utf-8"),
        tools=synthesize_tools(pair_scope, owner_scope, clock),
        output_schema=SynthesizeOutput,
        output_key="synthesize_result",
        generate_content_config=genai_types.GenerateContentConfig(temperature=0),
    )
