"""The /mentor review pull path: enters at gather directly, bypassing the
day/time gate AND the suppression check entirely (design spec §3: "the
Friday/time gate decides when to push automatically, never whether the
review can be pulled on demand" — same framing F1's pull_dossier gives
its own push-gate bypass). Always answers fresh: deliver_friday_review's
own "pull" branch updates the existing (owner, week, "pull") row in place
rather than deduping it away.

REAL BUG FOUND AND FIXED (confirmed live: pulling a fresh review still
showed "not enough categorized history yet" after that fix had already
landed in app.sub_agents.friday_review.agent's run_friday_review_flow) —
this is a deliberately SEPARATE gather->synthesize->deliver sequence, not
a call into run_friday_review_flow (see this module's own docstring on
why: it skips that flow's day/time gate and suppression check entirely).
That separateness meant the skill-classification persistence step landed
in run_friday_review_flow ALONE, never here — anyone testing via pull
(the natural "give me a fresh one right now" path) would never see it."""

import logging

from app.core.clock import Clock
from app.core.models import User
from app.core.scope import OwnerScope
from app.sub_agents.friday_review.agent import (
    persist_new_skill_classifications,
    refresh_card_with_this_weeks_classifications,
)
from app.sub_agents.friday_review.sub_agents.deliver.agent import deliver_friday_review
from app.sub_agents.friday_review.sub_agents.gather.agent import (
    gather_friday_review_context,
    week_start_monday,
)
from app.sub_agents.friday_review.sub_agents.synthesize.agent import (
    PROMPT_VERSION,
    build_synthesize_agent,
    synthesize_friday_review,
)

logger = logging.getLogger(__name__)


def pull_friday_review(
    owner_scope: OwnerScope, clock: Clock, llm_agent=None, deliverer=None
):
    context = gather_friday_review_context(owner_scope, clock)
    agent = llm_agent if llm_agent is not None else build_synthesize_agent()
    card = synthesize_friday_review(context, agent)

    try:
        newly_classified = persist_new_skill_classifications(owner_scope, card, clock)
        card = refresh_card_with_this_weeks_classifications(card, context, newly_classified)
    except Exception:
        logger.exception(
            "pull_friday_review: skill classification persistence failed, "
            "sending the card as synthesized"
        )

    user = owner_scope.session.get(User, owner_scope.owner_user_id)
    return deliver_friday_review(
        card, week_start=week_start_monday(clock.now()), trigger="pull",
        owner_scope=owner_scope, slack_user_id=user.slack_user_id,
        prompt_version=PROMPT_VERSION, deliverer=deliverer, clock=clock,
    )
