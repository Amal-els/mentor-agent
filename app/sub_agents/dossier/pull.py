"""The /mentor prep pull path: gather -> synthesize -> deliver, skipping
the push gate entirely (spec §2: "the gate decides whether to push, never
whether the dossier can be pulled" — pull must always be complete).

Same llm_agent fix Task 8 already made in app/sub_agents/dossier/agent.py's
run_dossier_flow (see that module's own docstring for the full reasoning):
the brief's sample has `pull_dossier(..., llm_agent=None, ...)` pass
llm_agent straight into synthesize_dossier(context, llm_agent), which calls
run_agent_sync(llm_agent, ...) unconditionally — a None agent crashes the
real ADK Runner. build_synthesize_agent() is built here when the caller
doesn't supply one, exactly as run_dossier_flow already does; llm_agent is
kept as an optional injection seam for tests/future callers, not dropped."""

from app.core.clock import Clock
from app.core.models import User
from app.core.scope import OwnerScope
from app.sub_agents.dossier.sub_agents.deliver.agent import deliver_dossier
from app.sub_agents.dossier.sub_agents.gather.agent import gather_dossier_context
from app.sub_agents.dossier.sub_agents.synthesize.agent import (
    PROMPT_VERSION,
    build_synthesize_agent,
    synthesize_dossier,
)


def pull_dossier(
    event: dict,
    owner_scope: OwnerScope,
    clock: Clock,
    connectors: dict,
    llm_agent=None,
    deliverer=None,
):
    context = gather_dossier_context(event, owner_scope, clock, connectors)

    # Built internally, not defaulted to None: synthesize_dossier calls
    # run_agent_sync(llm_agent, ...) unconditionally, and pull never gates
    # on candidate_score at all — every pull reaches this line. See module
    # docstring / app/sub_agents/dossier/agent.py's run_dossier_flow, which
    # made the identical fix for the push path.
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
        event_title=event.get("title"),
        event_starts_at=event.get("starts_at"),
    )
