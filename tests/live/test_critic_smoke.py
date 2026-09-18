"""Live smoke test for the critic LLM-as-judge (app/sub_agents/pulse/
sub_agents/critic/agent.py). Deselected by default (pyproject.toml's
`addopts = "-m 'not live'"`) — run explicitly with `uv run pytest -m live`
after setting GOOGLE_API_KEY or GEMINI_API_KEY.

These are the exact five scenarios the old rule-based evaluate() was
tested against before the LLM-as-judge rebuild (docs/plans/morning-pulse.md
notes the comparison). A live run confirmed the LLM judge matches every
expected verdict and, on missing_degradation_line, is strictly more
thorough than the old regex check — it also catches an unprovenanced name
leaking into `action`, not just `why_now`. Verdict is asserted exactly (a
closed pass/revise choice, unlike the ranker/writer's free-text output);
which item_ids get flagged is not asserted as strictly for scenarios where
the model could reasonably flag more than the original rule check did."""

import pytest

from app.core.llm import creds_available
from app.sub_agents.pulse.sub_agents.critic.agent import run_critic_agent
from tests.fixture_loading import load_critic_scenario

pytestmark = pytest.mark.live

_SKIP_REASON = "no LLM credentials configured (GOOGLE_API_KEY/GEMINI_API_KEY)"

SCENARIOS = [
    "clean_pass",
    "invented_name",
    "missing_why_now",
    "focus_count_exceeds_three",
    "missing_degradation_line",
]


@pytest.mark.skipif(not creds_available(), reason=_SKIP_REASON)
@pytest.mark.parametrize("name", SCENARIOS)
def test_live_critic_matches_expected_verdict(name):
    draft, context, expected = load_critic_scenario(name)

    result = run_critic_agent(draft, context)

    assert result.verdict == expected["verdict"], (name, result.reasons)


@pytest.mark.skipif(not creds_available(), reason=_SKIP_REASON)
def test_live_critic_flags_the_invented_name_item():
    draft, context, expected = load_critic_scenario("invented_name")

    result = run_critic_agent(draft, context)

    expected_item_ids = {r["item_id"] for r in expected["reasons"]}
    flagged_item_ids = {r.item_id for r in result.reasons}
    assert expected_item_ids <= flagged_item_ids


@pytest.mark.skipif(not creds_available(), reason=_SKIP_REASON)
def test_live_critic_clean_pass_has_no_reasons():
    draft, context, _expected = load_critic_scenario("clean_pass")

    result = run_critic_agent(draft, context)

    assert result.reasons == []
