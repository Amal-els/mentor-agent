"""ADK writer sub-agent — pulse_writer.v1 (docs/plans/morning-pulse.md M4).
A real LlmAgent (structured output, temperature 0). Runs standalone
(run_writer_agent, for isolated/live testing) or as the second step of
pulse_pipeline (app/sub_agents/pulse/agent.py's SequentialAgent, chained
after ranker_agent). Either way its output is never trusted as-is —
app/pipeline/pulse.py still asserts item_id order/membership matches the
input exactly afterward, in code.

Two ways this agent's input can arrive, unified by _prepare_writer_input
(a before_agent_callback so a single writer_agent definition serves both
callers):
- Standalone: the caller (run_writer_agent) already has fully-built
  WriterInputItem objects and seeds `writer_input_json` directly.
- Chained in pulse_pipeline: ranker_agent's output_key only produced
  ordered_item_ids + rationale (it never saw titles — pulse_ranker.v1.md's
  own contract). The caller instead seeds `item_titles_json` /
  `item_score_terms_json` (item_id -> title / score_terms, known upfront
  from the shortlist, before ranker even runs) and this callback merges
  those with ranker_agent's state output into the same `writer_input_json`
  shape right before writer_agent's own turn.

fallback_render/write/WriterInputItem/PulseItem below are the
deterministic, non-LLM side of the writer — see app/sub_agents/pulse/
sub_agents/ranker/agent.py's docstring for why fallback logic lives
alongside its agent rather than in app/pipeline/."""

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from google.adk.agents import Agent
from google.adk.agents.callback_context import CallbackContext
from google.genai import types as genai_types
from pydantic import BaseModel, Field

from app.core.adk_runner import run_agent_sync

MODEL = "gemini-3.5-flash-lite"
PROMPT_ID = "pulse_writer.v1"
_PROMPT_PATH = Path(__file__).resolve().parents[4] / "prompts" / "pulse_writer.v1.md"


class WriterDraftItem(BaseModel):
    item_id: str
    title: str
    why_now: str = Field(description="Every clause must trace to score_terms.")
    action: str


class WriterOutput(BaseModel):
    items: list[WriterDraftItem]


def _prepare_writer_input(callback_context: CallbackContext) -> None:
    state = callback_context.state
    if "ranker_result" not in state:
        return  # standalone caller already built writer_input_json directly

    # Checking for ranker_result's presence, not "already built", matters
    # specifically inside pulse_agent's LoopAgent: state persists across
    # iterations, so a presence check on writer_input_json itself would
    # find iteration 1's leftover value on iteration 2 and skip rebuilding
    # it against the fresh ranker_result — silently rewriting against
    # stale input instead of the retry's real re-ranking.

    ranker_result = state["ranker_result"]
    titles = json.loads(state["item_titles_json"])
    score_terms = json.loads(state["item_score_terms_json"])
    rationale = ranker_result["rationale"]
    # empty on a first attempt; on a pulse_agent loop retry, the critic's
    # per-item reasons from the previous iteration (see pulse_ranker.v1.md/
    # pulse_writer.v1.md's own "if you're seeing this a second time" notes)
    previous_critique = {
        r["item_id"]: r["reason"]
        for r in json.loads(state.get("previous_critique_json", "[]"))
        if r.get("item_id")
    }

    writer_input = [
        {
            "item_id": item_id,
            "title": titles.get(item_id, ""),
            "score_terms": score_terms.get(item_id, {}),
            "rationale": rationale.get(item_id, ""),
            "previous_critique": previous_critique.get(item_id, ""),
        }
        for item_id in ranker_result["ordered_item_ids"]
    ]
    state["writer_input_json"] = json.dumps(writer_input)


writer_agent = Agent(
    name="pulse_writer",
    model=MODEL,
    instruction=_PROMPT_PATH.read_text(encoding="utf-8")
    + "\n\nInput (JSON list, in the exact order to preserve):\n{writer_input_json}",
    before_agent_callback=_prepare_writer_input,
    output_schema=WriterOutput,
    output_key="writer_result",
    generate_content_config=genai_types.GenerateContentConfig(temperature=0),
)


def run_writer_agent(items: list) -> WriterOutput:
    """items: list[WriterInputItem]. Raises on any failure — the caller
    (llm_write below) decides whether to attempt this at all and catches
    failures itself."""
    writer_input = [
        {
            "item_id": item.item_id,
            "title": item.title,
            "score_terms": item.score_terms,
            "rationale": item.rationale,
            "previous_critique": "",
        }
        for item in items
    ]
    state = run_agent_sync(
        writer_agent, {"writer_input_json": json.dumps(writer_input)}
    )
    return WriterOutput.model_validate(state["writer_result"])


@dataclass(frozen=True)
class WriterInputItem:
    item_id: str
    title: str
    score_terms: dict
    rationale: str


@dataclass(frozen=True)
class PulseItem:
    item_id: str
    title: str
    why_now: str
    action: str
    # Never set by the writer/LLM — attached in code afterward (app/pipeline/
    # pulse.py's run_pulse), by matching item_id back to the shortlist's own
    # url. Keeping it out of the writer's output schema means there's no
    # path for the model to invent or garble a link.
    url: str | None = None
    # Same code-attached-after contract as url — a short "ticket key ·
    # owner" / "From sender" line built from the DB row and identity
    # resolution, never from LLM prose, so it can't invent a name or
    # reference a ticket that doesn't exist. See app/pipeline/pulse.py's
    # _fetch_detail.
    detail: str | None = None


def _why_now(item: WriterInputItem) -> str:
    terms = item.score_terms
    if terms.get("overdue"):
        clause = "Overdue"
    elif terms.get("due_today"):
        clause = "Due today"
    else:
        clause = "Flagged"
    person = terms.get("person_waiting")
    if person:
        clause = f"{clause} — {person} is waiting on this"
    # is_new_since_last_pulse is only ever set (True or False) for work
    # items/messages — absent (None) for events, which never carry the
    # term at all. Only prepend for an explicit False; matches
    # app/prompts/pulse_writer.v1.md's own rule for the LLM path, kept
    # here too since this deterministic template is what actually ships
    # when the LLM path is unavailable or fails.
    if terms.get("is_new_since_last_pulse") is False:
        return f"Still open since your last pulse — {clause[0].lower()}{clause[1:]}"
    return clause


def _action(item: WriterInputItem) -> str:
    terms = item.score_terms
    if terms.get("overdue"):
        return "Deliver this today — it's already late"
    if terms.get("due_today"):
        return "Handle this before end of day"
    return "Make progress on this"


def fallback_render(items: list[WriterInputItem]) -> list[PulseItem]:
    """Deterministic template render, no LLM — the step-6 fallback for
    'writer fails -> retry once, then template render'. Order-preserving
    and content-preserving by construction: a 1:1 map over the input."""
    return [
        PulseItem(
            item_id=item.item_id,
            title=item.title,
            why_now=_why_now(item),
            action=_action(item),
        )
        for item in items
    ]


def write(
    items: list[WriterInputItem],
    model_call: Callable[[list[WriterInputItem]], list[PulseItem]] | None = None,
) -> list[PulseItem]:
    if model_call is not None:
        try:
            drafted = model_call(items)
            if [d.item_id for d in drafted] == [i.item_id for i in items]:
                return drafted
        except Exception:
            pass
    return fallback_render(items)


def llm_write(items: list[WriterInputItem]) -> list[PulseItem]:
    """The real LLM path (temperature 0), via run_writer_agent above.
    Raises on any failure — the caller (app/pipeline/pulse.py) decides
    whether to attempt this at all (only when
    app.core.llm.creds_available()) and catches failures itself, so a
    failed call is distinguishable from "never attempted"."""
    result = run_writer_agent(items)
    return [
        PulseItem(
            item_id=entry.item_id,
            title=entry.title,
            why_now=entry.why_now,
            action=entry.action,
        )
        for entry in result.items
    ]
