"""ADK DeliverSummaryAgent — third step of the post-meeting
SequentialAgent (design spec §4.4c). Hybrid: phrase_agent (LlmAgent)
rewrites item text; assemble_summary (plain function) is the only thing
that ever calls build_a2ui_payload — the LLM never emits A2UI JSON
directly, matching app.agenda.payload's own docstring."""

import json
from pathlib import Path

from google.adk.agents import Agent
from google.adk.agents.callback_context import CallbackContext
from google.genai import types as genai_types
from pydantic import BaseModel

from app.agenda.payload import build_a2ui_payload
from app.agenda.scope import PairScope
from app.agenda.store import get_full_agenda

MODEL = "gemini-3.5-flash-lite"
PROMPT_ID = "agenda_deliver.v1"
_PROMPT_PATH = Path(__file__).resolve().parents[4] / "prompts" / "agenda_deliver.v1.md"


class PhraseOutput(BaseModel):
    phrased: dict[str, str]


def _item_to_dict(item) -> dict:
    return {
        "id": item.id,
        "text": item.text,
        "source": item.source,
        "visibility": item.visibility,
        "status": item.status,
    }


def _build_items_json_callback(pair_scope: PairScope):
    """Factory, not a plain before_agent_callback: phrase_agent needs to
    know which items to phrase, and that list only exists behind a DB
    call (get_full_agenda(pair_scope)) — pair_scope isn't JSON-
    serializable, so it can't be seeded into session state the way
    ranker_input_json/writer_input_json are (see pulse's ranker/writer
    agents). Instead this returns a closure over this call's specific
    pair_scope, to be attached as phrase_agent's before_agent_callback
    per invocation (run_post_meeting_flow clones phrase_agent per call
    anyway, for the same "can't share a parented singleton across calls"
    reason).

    Uses get_full_agenda, not get_agenda: phrasing must cover every item
    regardless of visibility, or a report_only/manager_only item ends up
    permanently unphrased for whichever party's recipient-facing delivery
    later includes it (send_post_meeting_slack_dm re-filters per actual
    recipient separately and correctly — this is purely about which items
    the LLM gets a chance to rewrite the text of).

    Excludes pending_consent items: those get a separate consent_card
    treatment in app.agenda.payload, never phrasing — matching
    run_post_meeting_flow's own post-SequentialAgent split of the same
    agenda into items vs. pending_consent_items."""

    def _prepare_phrase_input(callback_context: CallbackContext) -> None:
        agenda = get_full_agenda(pair_scope)
        # "open", not "!= pending_consent" (see run_post_meeting_flow's
        # matching fix) — a resolved item has no business being phrased
        # as if it were still an active agenda item.
        items = [_item_to_dict(item) for item in agenda if item.status == "open"]
        callback_context.state["items_json"] = json.dumps(items)

    return _prepare_phrase_input


phrase_agent = Agent(
    name="agenda_phrase",
    model=MODEL,
    instruction=_PROMPT_PATH.read_text(encoding="utf-8")
    + "\n\nItems (JSON list, id -> raw text):\n{items_json}",
    output_schema=PhraseOutput,
    output_key="phrase_result",
    generate_content_config=genai_types.GenerateContentConfig(temperature=0),
)


def assemble_summary(
    items: list[dict], pending_consent_items: list[dict], phrased_text: dict[str, str]
) -> dict:
    """The only function that calls build_a2ui_payload — applies
    phrase_agent's rewritten text where available, falls back to the raw
    item text otherwise (e.g. phrase_agent failed or an item wasn't
    included in its output)."""
    rendered_items = [
        {**item, "text": phrased_text.get(item["id"], item["text"])} for item in items
    ]
    return build_a2ui_payload(rendered_items, pending_consent_items)
