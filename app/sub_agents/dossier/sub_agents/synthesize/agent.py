"""Layer 6 composition: LLM synthesis of the 5-section dossier card, plus
the provenance drop rule (no talking point without a source_link).

llm_agent is injected by the caller (app/sub_agents/dossier/agent.py's
run_dossier_flow, per the plan's Task 8/9) rather than built in this
module — matching this function's declared interface,
`synthesize_dossier(context, llm_agent)`. Whoever builds that agent must
configure it with `output_schema=SynthesizeOutput` and
`output_key=OUTPUT_KEY` — the same output_schema/output_key contract
every other LLM-calling sub-agent in this codebase follows
(app/sub_agents/pulse/sub_agents/ranker/agent.py's ranker_agent,
.../writer/agent.py's writer_agent, app/delivery/tts.py's narrator_agent
all read their result back out of session state at a fixed output_key,
never treat run_agent_sync's return value as the parsed output directly)
— and load its instruction from app/prompts/dossier_synthesize.md
(PROMPT_VERSION, recorded on the resulting DossierDelivery row per
AGENT.md §3's versioned-prompt rule).

Second half of the contract, easy to miss: this module seeds the LLM
session's initial state under the key "context_json" (see
_context_to_state below), but ADK only substitutes a state key into the
model's prompt if the agent's own instruction text literally references
it as `{context_json}` — the same way ranker_agent/writer_agent/
narrator_agent each append their own state-key placeholder to their
loaded instruction in their own file. dossier_synthesize.md contains no
such placeholder (a static prompt, not built per-agent in this module),
so whoever builds llm_agent in Task 8/9 must append something like
`+ "\n\nContext:\n{context_json}"` to the loaded prompt text — otherwise
the model runs with no context at all and this fails silently (no
error, just an ungrounded response)."""

import dataclasses
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from google.adk.agents import Agent
from google.genai import types as genai_types
from pydantic import BaseModel

from app.sub_agents.dossier.sub_agents.gather.agent import DossierContext

PROMPT_VERSION = "dossier_synthesize@v6"
PROMPT_PATH = Path(__file__).resolve().parents[4] / "prompts" / "dossier_synthesize.md"

# REAL CHANGE (requested): bumped from gemini-3.5-flash-lite to
# gemini-3.7-flash for this ritual specifically — the pre-meeting dossier
# synthesis is the one LLM call in this sequence and benefits from the
# newer, more capable Flash tier. Every other sub-agent in this codebase
# (agenda synthesize, friday_review synthesize, etc.) still uses
# gemini-3.5-flash-lite deliberately — this is a scoped, single-ritual
# change, not a codebase-wide model bump.
MODEL = "gemini-3.7-flash"

# The session-state key the injected llm_agent must publish its
# structured output under (its own output_key) — see module docstring.
OUTPUT_KEY = "dossier_synthesize_result"


@dataclass(frozen=True)
class TalkingPoint:
    text: str
    source_link: str | None


@dataclass(frozen=True)
class DossierCard:
    who: list[str]
    why_now: str
    talking_points: list[TalkingPoint]
    promised_and_not_delivered: list[str]
    # Interpersonal blockers: a resolved attendee's own WorkItem sitting
    # in status="blocked" — gather/agent.py's _blocked_work_items_with_
    # attendees. Deterministic strings, same "DB row wins over LLM guess"
    # precedent as promised_and_not_delivered below, never LLM-phrased.
    blockers: list[str]
    suggested_opener: str | None
    short_version: bool
    nothing_to_prep: bool


class _TalkingPointOutput(BaseModel):
    text: str
    source_link: str | None = None


class SynthesizeOutput(BaseModel):
    """Structured output contract the injected llm_agent must be built
    with (output_schema=SynthesizeOutput, output_key=OUTPUT_KEY)."""

    who: list[str]
    why_now: str
    talking_points: list[_TalkingPointOutput]
    promised_and_not_delivered: list[str]
    suggested_opener: str | None = None
    short_version: bool = False


def build_synthesize_agent() -> Agent:
    """Factory for the real dossier-synthesis Agent, added in Task 8
    (the orchestrator, app/sub_agents/dossier/agent.py's run_dossier_flow)
    per this module's own docstring, which explicitly deferred building
    llm_agent to Task 8/9. Mirrors
    app.sub_agents.agenda.sub_agents.synthesize.agent.build_synthesize_agent's
    shape: built fresh per call (cheap — no tools/scopes to close over
    here, unlike agenda's version, so there's no correctness reason to
    call this more than once per process, but matching the "build fresh"
    precedent keeps the two modules easy to compare), with the loaded
    prompt text plus an appended `{context_json}` reference so ADK
    actually substitutes _context_to_state's seeded state key into the
    model's prompt (see this module's docstring, "Second half of the
    contract, easy to miss") — omitting this appended reference leaves the
    model with no context and no error, which is why it's called out
    explicitly rather than trusted to the static prompt file alone."""
    instruction = (
        PROMPT_PATH.read_text(encoding="utf-8") + "\n\nContext:\n{context_json}"
    )
    return Agent(
        name="dossier_synthesize",
        model=MODEL,
        instruction=instruction,
        output_schema=SynthesizeOutput,
        output_key=OUTPUT_KEY,
        generate_content_config=genai_types.GenerateContentConfig(temperature=0),
    )


def _is_real_url(value: str | None) -> bool:
    return value is not None and (
        value.startswith("http://") or value.startswith("https://")
    )


def drop_unsourced_talking_points(points: list[TalkingPoint]) -> list[TalkingPoint]:
    """The provenance rule: a claim with no source_link is not a talking
    point (dossier_synthesize.md's own instruction to the model), enforced
    here in code rather than trusted from the LLM's output alone.

    Truthiness alone isn't enough: caught live with open_commitments in
    context (real Commitment rows, each carrying its own DB "id" field for
    _format_open_commitment's use, see gather/agent.py) — the model
    sometimes phrases a commitment as a talking point and, needing
    *something* non-empty for source_link, copies that internal id
    across. A bare UUID isn't a link anyone can click; requiring an
    http(s) scheme catches this (and anything else non-URL) regardless of
    whether the prompt's own instruction against it is followed."""
    return [p for p in points if _is_real_url(p.source_link)]


def _agenda_item_to_dict(item) -> dict:
    # AgendaItem (app/agenda/models.py) is a SQLAlchemy declarative model,
    # not a dataclass — dataclasses.asdict() doesn't apply to it, so its
    # JSON-safe fields are pulled out by hand here, matching
    # app.sub_agents.agenda.sub_agents.synthesize.agent's own
    # _item_to_dict helper for the same model.
    return {
        "id": item.id,
        "text": item.text,
        "source": item.source,
        "source_link": item.source_link,
        "status": item.status,
        "surfaced_count": item.surfaced_count,
    }


def _context_to_state(context: DossierContext) -> dict[str, Any]:
    """JSON-safe view of DossierContext for seeding the LLM session's
    initial state. dataclasses.asdict recurses through the nested
    ResolvedAttendee/Resolution dataclasses; default=str below covers any
    other non-JSON-native value (e.g. a datetime nested in event or
    raw_signals) rather than crashing on it."""
    return {
        "context_json": json.dumps(
            {
                "event": context.event,
                "resolved_attendees": [
                    dataclasses.asdict(a) for a in context.resolved_attendees
                ],
                "agenda_carryover": (
                    [_agenda_item_to_dict(item) for item in context.agenda_carryover]
                    if context.agenda_carryover
                    else None
                ),
                "open_commitments": context.open_commitments,
                "blocked_work_items": context.blocked_work_items,
                "raw_signals": context.raw_signals,
            },
            default=str,
        )
    }


def _format_open_commitment(item: dict) -> str:
    """Real Commitment row -> display string. `description` already
    carries the direction (who owes what) in its own natural-language
    text — Commitment has no created_by_role column (see gather/agent.py's
    _open_commitments_with_attendees docstring), so this never invents a
    direction, only appends the counterpart name and due/overdue info."""
    text = item["description"]
    who = item.get("promised_to_name")
    if who:
        text = f"{text} (with {who})"
    if item.get("overdue"):
        text = f"{text} — overdue"
    elif item.get("due_at"):
        text = f"{text} — due {item['due_at']}"
    return text


def _format_blocked_work_item(item: dict) -> str:
    """Real WorkItem row -> display string, same discipline as
    _format_open_commitment: code-formatted from structured fields only,
    never LLM prose, so it can't invent a reason or overstate what's
    actually just a status field and a name."""
    ticket = item.get("external_id")
    title = item.get("title") or ticket or "Untitled item"
    text = f"{ticket} — {title}" if ticket and item.get("title") else title
    who = item.get("person_name")
    if who:
        text = f"{text} (blocked — {who})"
    else:
        text = f"{text} (blocked)"
    days_stale = item.get("days_stale")
    if days_stale is not None and days_stale > 0:
        text = f"{text}, {days_stale}d"
    return text


def synthesize_dossier(context: DossierContext, llm_agent) -> DossierCard:
    from app.core.adk_runner import run_agent_sync

    state = run_agent_sync(
        llm_agent,
        _context_to_state(context),
        kickoff_text="Write the dossier for this context.",
    )
    result = SynthesizeOutput.model_validate(state[OUTPUT_KEY])

    if context.agenda_carryover:
        # Reuse existing agenda items verbatim instead of the model's
        # invented points (dossier_synthesize.md's own instruction).
        # drop_unsourced_talking_points' http(s)-only check does NOT apply
        # here: AgendaItem.source_link is overloaded (app/agenda/store.py's
        # append_ledger_item) — for a jira_blocker/slack_q item it's a real
        # clickable URL, but for a ledger-mirrored commitment/accomplishment
        # it's that row's own internal id, used only as a dedup key, never
        # meant to be a link. These are real, DB-backed ledger data either
        # way (same trust precedent as open_commitments below) — the strict
        # URL check exists to catch the LLM inventing a fake source for a
        # FRESH point, not to second-guess what the agenda store already
        # has on file. Regression found live: this filter previously ran
        # unconditionally and silently dropped every commitment-sourced
        # carryover item, collapsing a real agenda into "nothing to prep."
        talking_points = [
            TalkingPoint(text=item.text, source_link=item.source_link)
            for item in context.agenda_carryover
        ]
    else:
        talking_points = [
            TalkingPoint(text=tp.text, source_link=tp.source_link)
            for tp in result.talking_points
        ]
        talking_points = drop_unsourced_talking_points(talking_points)

    # Real Commitment rows (gather/agent.py's _open_commitments_with_
    # attendees) take precedence over the LLM's guess off raw Slack/Linear
    # text, same "reuse verbatim instead of inventing" precedence already
    # applied to agenda_carryover above — and unlike the LLM's guess, these
    # are DB-sourced, closing the provenance gap this section otherwise had
    # (SynthesizeOutput.promised_and_not_delivered carries no source_link
    # at all).
    promised_and_not_delivered = result.promised_and_not_delivered
    if context.open_commitments:
        promised_and_not_delivered = [
            _format_open_commitment(item) for item in context.open_commitments
        ]

    blockers = [
        _format_blocked_work_item(item) for item in context.blocked_work_items
    ]

    return DossierCard(
        who=result.who,
        why_now=result.why_now,
        talking_points=talking_points,
        promised_and_not_delivered=promised_and_not_delivered,
        blockers=blockers,
        suggested_opener=result.suggested_opener,
        short_version=result.short_version,
        nothing_to_prep=(
            len(talking_points) == 0
            and not promised_and_not_delivered
            and not blockers
        ),
    )
