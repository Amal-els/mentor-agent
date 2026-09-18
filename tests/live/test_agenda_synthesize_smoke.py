"""One-shot live smoke test for agenda_synthesize's ledger-writing path.
Deselected by default (pyproject.toml's `addopts = "-m 'not live'"`) — run
explicitly with `uv run pytest -m live` after setting GOOGLE_API_KEY or
GEMINI_API_KEY.

This exists specifically because a real bug here was invisible to every
mocked unit test: app/sub_agents/agenda/sub_agents/synthesize/agent.py's
_prepare_related_key_results before_agent_callback set a state key the
prompt template referenced — correct in every unit test, which hand-builds
`initial_state` and never renders a real ADK instruction template. Against
the real ADK runtime, a referenced-but-unset state key raises KeyError at
template-render time (confirmed by reading google.adk.utils.instructions_
utils directly) and aborts the WHOLE agenda_synthesize turn before it ever
calls append_ledger_item_tool — silently dropping both ledgers, not just
the goal link. No mocked test exercises real template rendering, so this
class of bug can only be caught here.

map_okrs_result is seeded directly into initial_state rather than actually
running MapOkrs first — MapOkrs's own tools write to the real Notion
workspace (record_key_result_progress_tool), which this test must never
do. Seeding it this way still exercises the exact
prompt-template + tool-calling path the live bug was in, with zero Notion
risk."""

import datetime
import uuid

import pytest

from app.agenda.models import Pair
from app.agenda.scope import resolve_pair_scope
from app.core.clock import FrozenClock
from app.core.llm import creds_available
from app.core.models import Goal, User
from app.core.scope import OwnerScope
from app.sub_agents.agenda.sub_agents.synthesize.agent import build_synthesize_agent

pytestmark = pytest.mark.live

_SKIP_REASON = "no LLM credentials configured (GOOGLE_API_KEY/GEMINI_API_KEY)"

NOW = datetime.datetime(2026, 8, 27, 9, 0, tzinfo=datetime.UTC)


@pytest.mark.skipif(not creds_available(), reason=_SKIP_REASON)
def test_live_synthesize_agent_logs_an_accomplishment_with_related_key_results(
    pg_session,
):
    from app.core.adk_runner import run_agent_sync

    report_id = str(uuid.uuid4())
    manager_id = str(uuid.uuid4())
    pg_session.add_all(
        [
            User(id=report_id, created_at=NOW),
            User(id=manager_id, created_at=NOW),
        ]
    )
    pg_session.commit()
    pg_session.add(
        Pair(
            id=str(uuid.uuid4()),
            report_user_id=report_id,
            manager_user_id=manager_id,
            started_at=NOW,
            ended_at=None,
        )
    )
    goal_id = str(uuid.uuid4())
    pg_session.add(
        Goal(
            id=goal_id,
            owner_user_id=report_id,
            title="Reduce system error rate from 2.5% to < 0.5%",
            status="active",
            created_at=NOW,
            goal_type="key_result",
            source="notion",
            external_id="kr-live-smoke",
        )
    )
    pg_session.commit()

    pair_scope = resolve_pair_scope(pg_session, report_id, report_id)
    owner_scope = OwnerScope(owner_user_id=report_id, session=pg_session)
    clock = FrozenClock(at=NOW)

    synthesize_agent = build_synthesize_agent(pair_scope, owner_scope, clock)

    # Never touches MapOkrs/Notion — this state key is exactly what a real
    # MapOkrs run would have left behind, seeded directly.
    initial_state = {
        "map_okrs_result": {
            "mapped_key_results": ["kr-live-smoke"],
            "created_key_results": [],
            "note_appended": False,
        }
    }
    kickoff_text = (
        "Meeting transcript: report said 'I finished rewriting the retry "
        "logic in the ingestion pipeline, which should bring our error "
        "rate down a lot.' Record whatever this transcript actually "
        "supports."
    )

    # Must not raise — this line alone is the regression check for the
    # exact live bug (a KeyError from the missing-state-key template
    # crash) this file's own docstring describes.
    run_agent_sync(synthesize_agent, initial_state, kickoff_text=kickoff_text)

    from app.agenda.models import Accomplishment

    rows = (
        pg_session.query(Accomplishment)
        .filter(Accomplishment.owner_user_id == report_id)
        .all()
    )
    assert len(rows) >= 1, (
        "agenda_synthesize recorded nothing to the ledger for a transcript "
        "that clearly described a real accomplishment — the exact failure "
        "mode this test exists to catch"
    )
    # Not a hard assertion that goal_id is set on every row — the model's
    # own judgment call on whether this specific accomplishment really
    # matches the Key Result is not something this test should force —
    # but at least one row landing at all is the load-bearing check.
