"""L6 orchestrator (docs/plans/morning-pulse.md M4). The only module that
touches the DB or connectors; owns OwnerScope, budget, retries, and
logging. Sequential: L5 -> ranker+writer+critic (one ADK LoopAgent call,
app/sub_agents/pulse/agent.py's pulse_agent — critic can send ranker+
writer back for one retry, informed by its own critique) -> checks ->
PulseContext.

Ranker, writer, and critic now run as one real LoopAgent call — a failure
anywhere in it falls back to both deterministic paths together (ranker to
fallback_rank, writer to fallback_render), not just the stage that failed.
A ranker closed-set violation (enforce_ranker_output, still code, still
run after the agent call) discards the whole agent draft too, since it was
built against the rejected (pre-enforcement) item list — same reasoning
for why a writer order/membership mismatch discards it as well. Deliberate
trade-off for genuine ADK state-passing across all three agents, not an
oversight; fallback_rank/fallback_render remain fully safe outcomes either
way, and enforce_ranker_output plus the writer output/order assertion stay
unconditionally code, never trusted to any agent, applied to whatever the
loop's final iteration produced."""

import dataclasses
import datetime
import hashlib
import json
import logging
import time
from dataclasses import dataclass

from app.core.clock import Clock
from app.core.llm import creds_available
from app.core.models import Event, Message, WorkItem
from app.core.scope import OwnerScope
from app.identity.models import Person
from app.salience.pulse.pulse_context import build_pulse_context
from app.salience.pulse.types import PulseContext, ScoredItem
from app.sub_agents.pulse.agent import run_pulse_agent
from app.sub_agents.pulse.sub_agents.ranker.agent import (
    MAX_FOCUS,
    RankerResult,
    fallback_rank,
)
from app.sub_agents.pulse.sub_agents.writer.agent import (
    PulseItem,
    WriterInputItem,
    fallback_render,
)

logger = logging.getLogger(__name__)

RANKER_PROMPT_ID = "pulse_ranker.v1"
WRITER_PROMPT_ID = "pulse_writer.v1"
# Was "critic_rules.v1" back when the critic was rule-based code that never
# called a model. Now a real prompt version for a real LLM judge — matches
# app/sub_agents/pulse/sub_agents/
# critic/agent.py's PROMPT_ID.
CRITIC_PROMPT_ID = "pulse_critic.v1"


def enforce_ranker_output(
    shortlist: list[ScoredItem], result: RankerResult
) -> tuple[RankerResult, bool]:
    """Immediately after the ranker returns (per the M4 spec, deliberately
    not part of the ranker's own prompt or code — trusting a model to police
    itself is how this kind of bug survives to production):

    - closed set: any id absent from the shortlist is a hard fail — the
      *entire* ranker result is discarded and replaced with fallback_rank's
      plain L5 score order, logged.
    - no events in Focus: same hard-fail/fallback treatment as an unknown
      id — every event already shows in the card's own Today's meeting
      load section regardless of ranking, so one winning a Focus slot
      too is never correct output, not just a low-quality pick the LLM
      ranker should generally avoid. fallback_rank's own item_type filter
      is what actually enforces this on the fallback path; this is what
      catches the LLM ranker choosing one anyway.
    - hard cut: truncate to MAX_FOCUS in code regardless of what came back.
    - no dupes, and every surviving id traces back to the shortlist.

    Returns (result, fell_back) — fell_back is True only on a closed-set/
    event-in-focus violation path, so callers can log/record the
    degradation.
    """
    valid_ids = {item.item_id for item in shortlist}
    unknown = [
        item_id for item_id in result.ordered_item_ids if item_id not in valid_ids
    ]
    if unknown:
        logger.warning(
            "ranker returned unknown item_id(s) %s, not in shortlist %s — "
            "falling back to L5 score order",
            unknown,
            sorted(valid_ids),
        )
        return fallback_rank(shortlist), True

    event_ids = {item.item_id for item in shortlist if item.item_type == "event"}
    picked_events = [
        item_id for item_id in result.ordered_item_ids if item_id in event_ids
    ]
    if picked_events:
        logger.warning(
            "ranker picked event id(s) %s for Focus — already covered by "
            "Today's meeting load, falling back to L5 score order",
            picked_events,
        )
        return fallback_rank(shortlist), True

    missing_rationale = [
        item_id
        for item_id in result.ordered_item_ids
        if item_id not in result.rationale
    ]
    if missing_rationale:
        logger.warning(
            "ranker returned item_id(s) %s with no rationale entry — "
            "falling back to L5 score order",
            missing_rationale,
        )
        return fallback_rank(shortlist), True

    deduped: list[str] = []
    for item_id in result.ordered_item_ids:
        if item_id not in deduped:
            deduped.append(item_id)
    cut = deduped[:MAX_FOCUS]

    return (
        RankerResult(
            ordered_item_ids=cut,
            rationale={item_id: result.rationale[item_id] for item_id in cut},
            prompt_id=result.prompt_id,
        ),
        False,
    )


def build_degradation_line(degraded_sources: list[str]) -> str | None:
    """Computed once, up front, from data the orchestrator already has —
    not left for the critic to catch reactively. The critic's own
    degradation-line check (app/sub_agents/pulse/sub_agents/critic/
    agent.py) stays as defense in depth, but in the normal path it
    never fires."""
    if not degraded_sources:
        return None
    return f"no {', '.join(degraded_sources)} — briefing on what else exists"


def context_hash(shortlist_view: list[dict]) -> str:
    canonical = json.dumps(shortlist_view, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


def _log_call(
    step: str,
    prompt_id: str,
    ctx_hash: str,
    temperature: float,
    started: float,
    token_count: int | None,
) -> None:
    logger.info(
        "pulse_call step=%s prompt_id=%s context_hash=%s temperature=%s "
        "latency_ms=%.1f token_count=%s",
        step,
        prompt_id,
        ctx_hash,
        temperature,
        (time.perf_counter() - started) * 1000,
        token_count,
    )


def _critic_context_view(context: PulseContext) -> dict:
    return {
        "shortlist": [
            {
                "item_id": item.item_id,
                "score": item.score,
                "score_terms": item.score_terms,
                "candidate_focus": item.candidate_focus,
            }
            for item in context.shortlist
        ],
        "degraded_sources": context.degraded_sources,
    }


def _message_title(row: Message) -> str:
    """Message rows carry no title/content column by design (AGENT.md
    privacy rule — never store raw message/comment text), so the title
    shown for a Slack mention or Google Docs comment is a generic,
    privacy-safe description built from structured metadata only, never
    from body_ref or any fetched content."""
    if row.source == "slack":
        return "Slack DM" if row.is_dm else "Slack mention"
    if row.source == "google_docs":
        return "Google Docs comment"
    return f"{row.source} message" if row.source else "Message"


def _model_for(item_type: str):
    if item_type == "message":
        return Message
    return Event if item_type == "event" else WorkItem


def _fetch_title(scope: OwnerScope, item: ScoredItem) -> str:
    row = scope.session.get(_model_for(item.item_type), item.item_id)
    if row is None:
        return ""
    if item.item_type == "message":
        return _message_title(row)
    return row.title or ""


def _fetch_url(scope: OwnerScope, item: ScoredItem) -> str | None:
    """A pointer to the real thing, so a generic title ("Slack mention",
    "Google Docs comment") doesn't leave the reader unable to tell which
    one it actually is — found live: a pulse with six same-scored, same-
    titled Slack mentions had no way to distinguish them without this."""
    row = scope.session.get(_model_for(item.item_type), item.item_id)
    return row.url if row is not None else None


def _fetch_detail(scope: OwnerScope, item: ScoredItem) -> str | None:
    """A short line giving enough context to judge relevance without
    opening the source — the ticket key and/or the resolved person tied to
    the item (assignee for a work item, sender for a message). Code-
    attached like url (see PulseItem.detail's own docstring): built from
    the DB row and identity resolution, never from LLM prose, so it can
    never invent a name or reference a ticket that doesn't exist. Events
    are skipped — the title itself already carries that context (e.g.
    "1:1 with Sarah")."""
    if item.item_type not in ("work_item", "message"):
        return None
    row = scope.session.get(_model_for(item.item_type), item.item_id)
    if row is None:
        return None

    person_name = None
    if row.resolved_person_id is not None:
        person = scope.session.get(Person, row.resolved_person_id)
        person_name = person.canonical_name if person is not None else None

    if item.item_type == "work_item":
        ticket = row.external_id
        if ticket and person_name:
            return f"{ticket} · {person_name}"
        return ticket or person_name

    return f"From {person_name}" if person_name else None


@dataclass(frozen=True)
class RenderedPulse:
    context: PulseContext
    items: list[PulseItem]
    ordered_item_ids: list[str]
    degradation_line: str | None
    ranker_prompt_id: str
    writer_prompt_id: str
    critic_prompt_id: str
    ranker_fell_back: bool
    critic_revised: bool
    critic_skipped: bool
    critic_reasons: list[tuple[str, str]]
    creds_present: bool
    ranker_used_llm: bool
    writer_used_llm: bool


def run_pulse(
    scope: OwnerScope,
    clock: Clock,
    source_clients: list,
    trigger: str,
    requested_at: datetime.datetime,
) -> RenderedPulse:
    context = build_pulse_context(scope, clock, source_clients, trigger, requested_at)
    ctx_hash = context_hash(
        [{"item_id": i.item_id, "score": i.score} for i in context.shortlist]
    )
    # The ranker/writer candidate pool, not context.shortlist itself —
    # every event, ranked or not, already shows in the card's own Today's
    # meeting load section (built independently from context.day_events),
    # so a meeting winning a Focus slot would just repeat information
    # already on the card. Filtered here, before the ranker/fallback ever
    # sees the pool, rather than relying on the ranker prompt to avoid
    # them (it structurally can't — item_type is never even sent to it,
    # only item_id/score/score_terms) or on enforce_ranker_output's own
    # after-the-fact check alone (still kept, as defense in depth).
    # context.shortlist itself is untouched — the full/uncapped shortlist
    # view, titles/urls/details, and the closed-set check below all still
    # need every candidate, events included.
    ranking_pool = [item for item in context.shortlist if item.item_type != "event"]
    creds_present = creds_available()

    all_titles = {item.item_id: _fetch_title(scope, item) for item in context.shortlist}
    all_urls = {item.item_id: _fetch_url(scope, item) for item in context.shortlist}
    all_details = {
        item.item_id: _fetch_detail(scope, item) for item in context.shortlist
    }
    degradation_line = build_degradation_line(context.degraded_sources)
    critic_context_view = _critic_context_view(context)

    ranker_used_llm = False
    writer_used_llm = False
    critic_revised = False
    critic_skipped = not creds_present
    critic_reasons: list[tuple[str, str]] = []
    agent_draft_items: list[PulseItem] | None = None
    started = time.perf_counter()
    if creds_present:
        try:
            agent_result = run_pulse_agent(
                ranking_pool, all_titles, critic_context_view, degradation_line
            )
            ranker_result = RankerResult(
                ordered_item_ids=list(agent_result.ranker.ordered_item_ids),
                rationale=dict(agent_result.ranker.rationale),
                prompt_id=RANKER_PROMPT_ID,
            )
            agent_draft_items = [
                PulseItem(
                    item_id=i.item_id, title=i.title, why_now=i.why_now, action=i.action
                )
                for i in agent_result.writer.items
            ]
            ranker_used_llm = True
            writer_used_llm = True
            critic_revised = agent_result.critic_revised
            critic_reasons = agent_result.critic_reasons
        except Exception:
            logger.warning(
                "llm pulse agent call failed despite credentials present, "
                "falling back to deterministic ranker+writer owner=%s",
                scope.owner_user_id,
                exc_info=True,
            )
            ranker_result = fallback_rank(ranking_pool)
            critic_skipped = True
    else:
        ranker_result = fallback_rank(ranking_pool)
    # ranker/writer/critic now run as one LoopAgent call (app/sub_agents/
    # pulse/agent.py's pulse_agent) — a single combined log entry reflects
    # that; there's no longer a separate per-step wall-clock split to log.
    _log_call(
        "pulse_agent",
        f"{RANKER_PROMPT_ID}+{WRITER_PROMPT_ID}+{CRITIC_PROMPT_ID}",
        ctx_hash,
        0,
        started,
        None,
    )

    checked_result, ranker_fell_back = enforce_ranker_output(
        context.shortlist, ranker_result
    )
    if ranker_fell_back:
        logger.warning("ranker_fell_back=true owner=%s", scope.owner_user_id)
        ranker_used_llm = False  # closed-set violation discarded the LLM output
        # the agent's whole draft (ranking+writing+critique) was built
        # against the rejected item list — none of it applies to the
        # corrected one, so the writer no longer gets to keep its LLM
        # output either, and the critic's verdict on it is moot too.
        agent_draft_items = None
        writer_used_llm = False
        critic_revised = False
        critic_reasons = []
        critic_skipped = True

    by_id = {item.item_id: item for item in context.shortlist}
    writer_inputs = [
        WriterInputItem(
            item_id=item_id,
            title=all_titles[item_id],
            score_terms=by_id[item_id].score_terms,
            rationale=checked_result.rationale.get(item_id, ""),
        )
        for item_id in checked_result.ordered_item_ids
    ]

    if agent_draft_items is not None and [
        d.item_id for d in agent_draft_items
    ] == [i.item_id for i in writer_inputs]:
        draft_items = agent_draft_items
    else:
        if agent_draft_items is not None:
            logger.warning(
                "llm writer reordered/dropped items, discarding and falling "
                "back to template render owner=%s",
                scope.owner_user_id,
            )
        writer_used_llm = False
        critic_revised = False
        critic_reasons = []
        critic_skipped = True
        draft_items = fallback_render(writer_inputs)

    # agents love to "help" by quietly reordering or merging items — the
    # prompt prohibits it, but this is the assertion that actually holds it.
    assert [
        i.item_id for i in draft_items
    ] == checked_result.ordered_item_ids, (
        "writer output order/membership diverged from the ranker's ordered_item_ids"
    )

    # code-attached, never LLM-attached (see PulseItem.url/detail's own docstrings)
    draft_items = [
        dataclasses.replace(
            item, url=all_urls.get(item.item_id), detail=all_details.get(item.item_id)
        )
        for item in draft_items
    ]

    if critic_revised:
        logger.info(
            "critic_revise owner=%s reasons=%s", scope.owner_user_id, critic_reasons
        )

    return RenderedPulse(
        context=context,
        items=draft_items,
        ordered_item_ids=[i.item_id for i in draft_items],
        degradation_line=degradation_line,
        ranker_prompt_id=RANKER_PROMPT_ID,
        writer_prompt_id=WRITER_PROMPT_ID,
        critic_prompt_id=CRITIC_PROMPT_ID,
        ranker_fell_back=ranker_fell_back,
        critic_revised=critic_revised,
        critic_skipped=critic_skipped,
        critic_reasons=critic_reasons,
        creds_present=creds_present,
        ranker_used_llm=ranker_used_llm,
        writer_used_llm=writer_used_llm,
    )
