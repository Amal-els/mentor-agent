import datetime
import logging
from google.adk.agents import BaseAgent
from google.adk.events import Event
from google.adk.events.event_actions import EventActions
from sqlalchemy import select
from app.agenda.scope import get_current_pair
from app.core.clock import Clock
from app.core.models import DossierDelivery, User
from app.core.scope import OwnerScope
from app.salience.dossier.gate import (
    DOSSIER_PUSH_BUDGET_MAX_PER_DAY,
    apply_dossier_gate,
)
from app.salience.dossier.score import DossierCandidateInputs, score_dossier_candidate
from app.sub_agents.dossier.sub_agents.deliver.agent import deliver_dossier
from app.sub_agents.dossier.sub_agents.gather.agent import gather_dossier_context
from app.sub_agents.dossier.sub_agents.synthesize.agent import (
    PROMPT_VERSION,
    build_synthesize_agent,
    synthesize_dossier,
)
logger = logging.getLogger(__name__)

_HISTORY_LOOKBACK_LIMIT = 30

def _real_is_manager(context, owner_scope: OwnerScope) -> bool:
    pair = get_current_pair(owner_scope.session, owner_scope.owner_user_id)
    if pair is None:
        return False
    for resolved_attendee in context.resolved_attendees:
        resolution = resolved_attendee.resolution
        person_id = getattr(resolution, "person_id", None)
        if person_id is not None and person_id == pair.manager_user_id:
            return True
    return False

def _recent_history_scores(owner_scope: OwnerScope, clock: Clock) -> list[float]:
    now = clock.now()
    rows = (
        owner_scope.session.execute(
            select(DossierDelivery.candidate_score)
            .where(
                DossierDelivery.owner_user_id == owner_scope.owner_user_id,
                DossierDelivery.candidate_score.is_not(None),
                DossierDelivery.created_at <= now,
            )
            .order_by(DossierDelivery.created_at.desc())
            .limit(_HISTORY_LOOKBACK_LIMIT)
        )
        .scalars()
        .all()
    )
    return list(rows)

def _todays_push_count(owner_scope: OwnerScope, clock: Clock) -> int:
    now = clock.now()
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + datetime.timedelta(days=1)
    rows = (
        owner_scope.session.execute(
            select(DossierDelivery.id).where(
                DossierDelivery.owner_user_id == owner_scope.owner_user_id,
                DossierDelivery.sent_at.is_not(None),
                DossierDelivery.sent_at >= day_start,
                DossierDelivery.sent_at < day_end,
            )
        )
        .scalars()
        .all()
    )
    return len(rows)

def run_dossier_flow(
    event: dict,
    owner_scope: OwnerScope,
    clock: Clock,
    connectors: dict,
    llm_agent=None,
    deliverer=None,
):
    context = gather_dossier_context(event, owner_scope, clock, connectors)
    inputs = DossierCandidateInputs(
        attendee_rarity=event.get("attendee_rarity", 0.5),
        is_external=event.get("is_external", False),
        unresolved_threads=len(context.raw_signals.get("slack", [])),
        deadline_proximity=event.get("deadline_proximity", 0.0),
        is_one_on_one=len(event.get("attendees", [])) <= 1,
        prep_absent=context.agenda_carryover is None,
        recurrence_familiarity=(
            1.0 if (event.get("is_recurring") or event.get("series_id")) else 0.0
        ),
    )
    candidate_score = score_dossier_candidate(inputs)
    is_manager = event.get("is_manager", False) or _real_is_manager(
        context, owner_scope
    )
    decision = apply_dossier_gate(
        owner_scope,
        clock,
        candidate_score=candidate_score,
        event_external_id=event["external_id"],
        is_manager=is_manager,
        history_scores=_recent_history_scores(owner_scope, clock),
    )
    if decision != "push":
        return None
    
    if _todays_push_count(owner_scope, clock) >= DOSSIER_PUSH_BUDGET_MAX_PER_DAY:
        logger.info(
            "dossier push budget exhausted for owner=%s (max=%d/day); "
            "queueing (skipping) event=%s",
            owner_scope.owner_user_id,
            DOSSIER_PUSH_BUDGET_MAX_PER_DAY,
            event.get("external_id"),
        )
        return None
    agent = llm_agent if llm_agent is not None else build_synthesize_agent()
    card = synthesize_dossier(context, agent)
    talking_points_source = "agenda_carryover" if context.agenda_carryover else "fresh"
    user = owner_scope.session.get(User, owner_scope.owner_user_id)
    return deliver_dossier(
        card,
        event_external_id=event["external_id"],
        owner_scope=owner_scope,
        slack_user_id=user.slack_user_id,
        prompt_version=PROMPT_VERSION,
        talking_points_source=talking_points_source,
        deliverer=deliverer,
        clock=clock,
        candidate_score=candidate_score,
        event_title=event.get("title"),
        event_starts_at=event.get("starts_at"),
    )

class DossierOrchestrator(BaseAgent):
    """ADK-invoked entry point, mirroring RollingAgendaOrchestrator."""
    async def _run_async_impl(self, ctx):
        event = ctx.session.state["event"]
        owner_scope = ctx.session.state["owner_scope"]
        clock = ctx.session.state["clock"]
        connectors = ctx.session.state["connectors"]
        delivery = run_dossier_flow(event, owner_scope, clock, connectors)
        state_delta = {
            "dossier_delivery_id": delivery.id if delivery is not None else None
        }
        yield Event(author=self.name, actions=EventActions(state_delta=state_delta))