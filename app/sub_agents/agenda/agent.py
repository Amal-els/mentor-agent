"""Post-meeting SequentialAgent (design spec §4.4): Capture -> MapOkrs ->
Synthesize -> Deliver, fixed order, each step depending on the last.
MapOkrs (app/sub_agents/agenda/sub_agents/map_okrs/agent.py, added this
session) runs right after Capture — it needs the transcript Capture just
produced, and mapping it onto real Notion Key Results is independent of
Synthesize's own agenda/ledger writes, so ordering relative to Synthesize
is not correctness-critical either way; placed before it for locality
(both "read the transcript, act on it" steps grouped early). Also the
module RollingAgendaOrchestrator (Task 17) imports run_post_meeting_flow
from."""

import logging
import threading
from typing import ClassVar

from google.adk.agents import BaseAgent, SequentialAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event
from google.adk.events.event_actions import EventActions

from app.agenda.scope import PairScope
from app.agenda.store import (
    add_manual_note,
    append_agenda_item,
    append_ledger_item,
    get_agenda,
    get_full_agenda,
)
from app.core.adk_runner import run_agent_sync
from app.core.clock import Clock
from app.core.scope import OwnerScope
from app.sub_agents.agenda.sub_agents.capture.agent import (
    _build_fetch_transcript_tool,
    capture_agent,
)
from app.sub_agents.agenda.sub_agents.deliver.agent import (
    _build_items_json_callback,
    _item_to_dict,
    assemble_summary,
    phrase_agent,
)
from app.sub_agents.agenda.sub_agents.map_okrs.agent import build_map_okrs_agent
from app.sub_agents.agenda.sub_agents.synthesize.agent import build_synthesize_agent

logger = logging.getLogger(__name__)


def run_post_meeting_flow(
    meeting_id: str,
    pair_scope: PairScope,
    owner_scope: OwnerScope,
    clock: Clock,
    transcript_text: str | None = None,
) -> dict:
    """transcript_text is optional, explicit, real content for this
    specific meeting — e.g. from a caller that already has one (the /ui
    demo dashboard's sample transcripts, or a future real ingestion
    source). Without it, Capture's fetch_transcript has no real backing
    store (see its own docstring) and falls back to "ask the user
    directly," which cannot actually work in this headless pipeline —
    confirmed live: produces zero agenda items. When given, it's wired
    onto a per-call clone of capture_agent's tools so it's never shared
    across calls."""
    synthesize_agent = build_synthesize_agent(pair_scope, owner_scope, clock)
    map_okrs_agent = build_map_okrs_agent(pair_scope, owner_scope, clock)
    # capture_agent and phrase_agent are module-level singletons (Task 12,
    # Task 15) shared across every call to this function. ADK's
    # SequentialAgent stamps parent_agent onto each sub-agent it's given,
    # and a BaseAgent that already has a parent raises a ValidationError
    # if handed to a second SequentialAgent — so reusing the singletons
    # directly means run_post_meeting_flow could only ever succeed once
    # per process. .clone() (which ADK provides for exactly this reuse
    # case) makes a fresh, parent-less copy each call instead.
    # phrase_agent's instruction template references {items_json}, but
    # nothing upstream in this SequentialAgent produces that list (capture
    # and synthesize don't emit "current agenda items"). The real source
    # is get_full_agenda(pair_scope) — a DB call, and pair_scope isn't
    # JSON-serializable, so it can't be pre-seeded into initial_state the
    # way plain-JSON template inputs are elsewhere. Attaching a
    # before_agent_callback via clone()'s update mapping closes over this
    # call's pair_scope and populates items_json right before phrase_agent
    # runs. See _build_items_json_callback's docstring for the
    # pending_consent exclusion rationale.
    phrase_agent_clone = phrase_agent.clone(
        {"before_agent_callback": _build_items_json_callback(pair_scope)}
    )
    capture_agent_clone = capture_agent.clone(
        {"tools": [_build_fetch_transcript_tool(transcript_text)]}
    )
    post_meeting_agent = SequentialAgent(
        name="agenda_post_meeting",
        sub_agents=[
            capture_agent_clone,
            map_okrs_agent,
            synthesize_agent,
            phrase_agent_clone,
        ],
    )

    # Captured BEFORE the pipeline runs, off the same clock every write
    # inside it stamps created_at with — the >= comparison below (not >)
    # matters specifically for FrozenClock in tests: a frozen instant
    # produces created_at == run_started_at exactly, and > would wrongly
    # exclude every item this very run just wrote.
    run_started_at = clock.now()
    state = run_agent_sync(
        post_meeting_agent,
        {"meeting_id": meeting_id},
        kickoff_text=f"Meeting {meeting_id} has ended. Begin.",
    )

    # get_full_agenda, not get_agenda: the internal pipeline (dedup during
    # synthesis, phrasing above, and this returned/state_delta payload)
    # must see every item regardless of visibility or which party
    # triggered meeting_end — a single acting-party view would leave
    # either report_only or manager_only items permanently invisible to
    # dedup/phrasing depending on who triggered the run. External,
    # recipient-facing reads are unaffected: send_post_meeting_slack_dm
    # below and the GET payload route both still go through
    # get_agenda(recipient_scope), which enforces is_visible_to.
    agenda = get_full_agenda(pair_scope)
    # "open", not "!= pending_consent": the latter also included resolved
    # items, which then never actually disappeared from the delivered
    # agenda/payload — a resolved item kept showing up as an editable,
    # still-active component forever. Only "open" items behave as active.
    items = [_item_to_dict(item) for item in agenda if item.status == "open"]
    pending_consent_items = [
        _item_to_dict(item) for item in agenda if item.status == "pending_consent"
    ]
    phrased = state.get("phrase_result", {}).get("phrased", {})
    summary = assemble_summary(items, pending_consent_items, phrased)
    # What actually came out of THIS meeting — genuinely new rows only
    # (created_at >= run_started_at), not an existing item that merely
    # got re-mentioned/bumped (dedup-by-source_link updates surfaced_
    # count in place, never created_at). REAL CHANGE (requested): the
    # post-meeting Slack DM used to always carry the FULL open agenda;
    # this is what lets it shrink down to "here's what this meeting
    # actually produced" instead — see send_post_meeting_slack_dm's own
    # only_item_ids docstring.
    new_item_ids = {
        item.id
        for item in agenda
        if item.status == "open" and item.created_at >= run_started_at
    }
    # Fire-and-forget, not awaited — REAL FIX (confirmed live, part of
    # the same latency-reduction pass as append_ledger_items_tool's
    # batching): send_post_meeting_slack_dm used to run inline here,
    # synchronously, as the LAST thing before this function returns —
    # meaning the whole pipeline's reported "done" time (and the polling
    # frontend's own wait, and agenda_scheduler's own poll-loop
    # timing) was held hostage by 1-2 real Slack API network calls that
    # don't block anything else. Every DB write this function makes
    # (capture/map_okrs/synthesize's agenda + ledger rows) is already
    # committed by this point — there is nothing left for the Slack send
    # to gate. See _send_post_meeting_slack_dm_in_background's own
    # docstring for why this can't just reuse pair_scope.session on the
    # new thread.
    threading.Thread(
        target=_send_post_meeting_slack_dm_in_background,
        args=(pair_scope.report_user_id, meeting_id, phrased, new_item_ids),
        daemon=True,
    ).start()
    return summary


def _send_post_meeting_slack_dm_in_background(
    report_user_id: str,
    meeting_id: str,
    phrased_text: dict[str, str] | None,
    only_item_ids: set[str] | None,
) -> None:
    """threading.Thread target for run_post_meeting_flow's own fire-and-
    forget Slack delivery (see that function's own comment). Opens and
    closes its OWN fresh DB session rather than reusing the caller's
    pair_scope.session — that session belongs to whichever request or
    FastAPI BackgroundTask called run_post_meeting_flow, and gets closed
    the moment that caller returns (app/triggers/agenda_router.py's
    _run_orchestrator finally block, right after run_post_meeting_flow's
    own return) — likely already closed, or at best a genuine cross-
    thread SQLAlchemy Session use, by the time this thread gets to run.
    Same "open your own session, don't inherit one across an async/thread
    boundary" pattern _run_orchestrator_in_background itself already uses
    for the exact same reason.

    Builds a plain self-access PairScope directly (report_user_id ==
    acting_user_id) rather than importing resolve_pair_scope for it —
    send_post_meeting_slack_dm only ever reads .session/.report_user_id
    off the scope it's given (confirmed by reading its own body: every
    real per-recipient scope it needs, it resolves fresh itself via
    resolve_pair_scope internally), so the passed-in scope's own
    acting_user_id has no real effect on what gets sent."""
    from app.core.config import get_settings
    from app.core.db import get_engine, get_session_factory

    session = get_session_factory(get_engine(get_settings().database_url))()
    try:
        pair_scope = PairScope(
            report_user_id=report_user_id,
            acting_user_id=report_user_id,
            session=session,
        )
        send_post_meeting_slack_dm(pair_scope, phrased_text, only_item_ids)
    except Exception:
        # The capture/synthesize DB writes already committed on the main
        # thread before this ever started — a Slack failure here must
        # never look like a pipeline failure (agenda_scheduler.py's
        # retry-on-False would otherwise re-run the whole LLM pipeline,
        # including real model calls, every poll until Slack recovers).
        logger.exception(
            "post-meeting Slack DM failed for report_user_id=%s, "
            "meeting_id=%s (agenda writes already committed, not "
            "retrying)",
            report_user_id,
            meeting_id,
        )
    finally:
        session.close()


def send_post_meeting_slack_dm(
    pair_scope: PairScope,
    phrased_text: dict[str, str] | None = None,
    only_item_ids: set[str] | None = None,
) -> dict:
    """Sends each side of the pair their own visibility-filtered view of
    the agenda as a Slack DM — never a single shared payload, since
    report_only/manager_only items must never reach the other party.
    Resolves a fresh PairScope per recipient and re-reads
    get_agenda(scope) for each, the same is_visible_to() choke-point
    every other agenda read in this codebase goes through, rather than
    reusing the flow's own single-viewpoint agenda list.

    only_item_ids, when given, restricts the DM to just those item ids —
    REAL CHANGE (requested): this used to always send the FULL open
    agenda on every post-meeting DM, which only gets longer and more
    repetitive meeting over meeting (yesterday's still-open items showing
    up again verbatim). run_post_meeting_flow now passes the set of item
    ids actually created THIS run (see its own "new_item_ids" comment),
    so the DM reads as a short "here's what came out of this meeting"
    summary instead of the whole agenda dump. None (the default) keeps
    the old full-agenda behavior — used by any caller that isn't a real
    meeting_end run (e.g. a manual re-send)."""
    from app.agenda.scope import get_current_pair, resolve_pair_scope
    from app.core.models import User
    from app.delivery.cards import build_agenda_summary_blocks
    from app.delivery.slack_deliverer import SlackDeliverer

    deliverer = SlackDeliverer()
    if not deliverer.enabled:
        return {"sent": False, "reason": "SLACK_BOT_TOKEN not configured"}

    if only_item_ids is not None and not only_item_ids:
        # A meeting that produced nothing new (only decisions/focus_points
        # were captured, no ledger item or fresh agenda entry) — skip the
        # DM entirely rather than send a technically-truthful but useless
        # "nothing here" card every single time. Distinct from "no active
        # pair"/"not configured" below: this is the expected, common case
        # for a short check-in, not a failure.
        return {"sent": False, "reason": "nothing new to summarize"}

    pair = get_current_pair(pair_scope.session, pair_scope.report_user_id)
    if pair is None:
        return {"sent": False, "reason": "no active pair"}

    report_user = pair_scope.session.get(User, pair.report_user_id)
    manager_user = pair_scope.session.get(User, pair.manager_user_id)
    phrased_text = phrased_text or {}

    def _recipient_items(acting_user_id: str) -> list[dict]:
        scope = resolve_pair_scope(
            pair_scope.session, pair.report_user_id, acting_user_id
        )
        agenda = get_agenda(scope)
        return [
            {**_item_to_dict(item), "text": phrased_text.get(item.id, item.text)}
            for item in agenda
            if item.status == "open"
            and (only_item_ids is None or item.id in only_item_ids)
        ]

    results = {}
    if report_user and report_user.slack_user_id:
        blocks = build_agenda_summary_blocks(_recipient_items(report_user.id))
        results["report"] = deliverer.deliver(report_user.slack_user_id, blocks)
    if manager_user and manager_user.slack_user_id:
        blocks = build_agenda_summary_blocks(_recipient_items(manager_user.id))
        results["manager"] = deliverer.deliver(manager_user.slack_user_id, blocks)

    return {"sent": True, "results": results}


class RollingAgendaOrchestrator(BaseAgent):
    """Pure routing, no LLM, no business logic (design spec §4.1). Reads
    trigger_type from session state and dispatches to one of three
    paths — the function-tool paths (ledger_event, manual_note) run
    directly here as plain calls, since they're explicitly "no agent"
    per spec §4.2/§4.3; only meeting_end delegates to the real
    SequentialAgent built in run_post_meeting_flow. If you're tempted to
    add dedup logic, schema validation, or consent-threshold logic here,
    it belongs one layer down in app.agenda.store instead.

    run_post_meeting_flow is a synchronous, blocking call (it drives its
    own Runner internally via run_agent_sync — see that module's
    docstring); calling it directly from this async generator blocks the
    event loop for the duration of the whole post-meeting SequentialAgent
    run. This mirrors the existing precedent in this codebase: pulse's
    run_pulse_agent is the same shape (a sync function wrapping its own
    Runner) and is called from sync pipeline code, never awaited either.
    Nothing here needs to `await` run_post_meeting_flow — it isn't a
    coroutine — so it's simply called like any other function."""

    pair_scope: PairScope
    owner_scope: OwnerScope
    clock: Clock

    model_config: ClassVar[dict] = {"arbitrary_types_allowed": True}

    async def _run_async_impl(self, ctx: InvocationContext):
        state = ctx.session.state
        trigger_type = state["trigger_type"]
        state_delta: dict = {}

        if trigger_type == "manual_note":
            item = add_manual_note(
                self.pair_scope,
                state["acting_user_id"],
                state["text"],
                state.get("visibility"),
                self.clock,
            )
            state_delta["agenda_item_id"] = item.id
        elif trigger_type == "ledger_event":
            kind = state["kind"]
            item_input = {
                "text": state["text"],
                "source": state["source"],
                "source_link": state.get("source_link"),
                "visibility": state.get("visibility", "shared"),
                "created_by_user_id": state["created_by_user_id"],
                "created_by_role": state["created_by_role"],
            }
            if kind in ("accomplishment", "commitment"):
                if kind == "accomplishment" and not state.get("occurred_at"):
                    # store.py's _parse_occurred_at has no default for
                    # occurred_at (unlike due_at on the commitment path,
                    # which is genuinely optional) — it's required for an
                    # accomplishment row. Catch a missing value here, at
                    # the boundary where the trigger payload is first
                    # parsed, rather than letting it fall through to an
                    # opaque TypeError from
                    # datetime.fromisoformat(None) deep inside store.py.
                    raise ValueError(
                        "occurred_at is required for accomplishment ledger events"
                    )
                ledger_item = {
                    "description": state["text"],
                    "created_by_user_id": state["created_by_user_id"],
                    "created_by_role": state["created_by_role"],
                    "promised_to_person_id": state.get("promised_to_person_id"),
                    "due_at": state.get("due_at"),
                    "occurred_at": state.get("occurred_at"),
                }
                item = append_ledger_item(
                    self.pair_scope, self.owner_scope, kind, ledger_item, self.clock
                )
            else:
                item = append_agenda_item(self.pair_scope, item_input, self.clock)
            state_delta["agenda_item_id"] = item.id
        elif trigger_type == "meeting_end":
            state_delta["agenda_payload"] = run_post_meeting_flow(
                state["meeting_id"],
                self.pair_scope,
                self.owner_scope,
                self.clock,
                transcript_text=state.get("transcript_text"),
            )
        else:
            raise ValueError(f"unknown trigger_type: {trigger_type!r}")

        # Mirrors _PulseGateStep's pattern exactly (app/sub_agents/pulse/
        # agent.py): a state change is only actually persisted by yielding
        # an Event carrying it as EventActions.state_delta — mutating
        # ctx.session.state directly, as an earlier draft of this method
        # did, does not reliably commit through ADK's event-sourced
        # session model. Every branch above yields exactly once, here.
        yield Event(author=self.name, actions=EventActions(state_delta=state_delta))
