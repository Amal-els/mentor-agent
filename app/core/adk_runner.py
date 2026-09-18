"""Sync bridge for invoking ADK agents from this codebase's synchronous L6
pipeline — the same asyncio.run() bridge app/tools/mcp_config.py already
uses for the (also asyncio-first) MCP SDK. One session per call, torn down
immediately after: these agents run once per pulse cycle, not on a hot
path, so there's no long-lived session to manage.

run_agent_sync() is called from two kinds of caller: plain sync code with
no event loop at all (CLI, a background thread) — asyncio.run() works
directly there — and, since app/tools/custom_tools.py's get_morning_pulse wired
run_pulse() into a real ADK conversational agent, from *inside* the ADK
Runner's own already-running event loop too. asyncio.run() raises
"cannot be called from a running event loop" in that second case; a live
run against root_agent caught this for real — it didn't crash (app/
pipeline/pulse.py's existing try/except caught the RuntimeError and fell
back to the deterministic path, as designed), but it meant the LLM path
was silently never attempted at all despite credentials being present —
exactly the "fallback indistinguishable from never attempted" gap
tests/live/test_llm_smoke.py exists to catch elsewhere. Fixed by detecting
a running loop and, if there is one, running the bridge in a fresh worker
thread with its own loop instead."""

import asyncio
import concurrent.futures
import uuid

from google.adk.agents import BaseAgent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from app.core.guardrails import GUARDRAIL_PLUGINS

APP_NAME = "mentor-pulse"


async def _run_agent_async(
    agent: BaseAgent, initial_state: dict, kickoff_text: str
) -> dict:
    session_service = InMemorySessionService()
    user_id = "pulse"
    session_id = str(uuid.uuid4())
    await session_service.create_session(
        app_name=APP_NAME,
        user_id=user_id,
        session_id=session_id,
        state=initial_state,
    )
    # plugins=GUARDRAIL_PLUGINS: this is the single dispatch point for every
    # L6 sub-agent pipeline (pulse ranker/writer/critic, dossier,
    # friday_review, agenda synthesize/capture/map_okrs/deliver, checklist,
    # mentor_advisor) — wiring the shared guardrail plugin (app/core/
    # guardrails.py) here covers all of them at once, rather than attaching
    # before_model_callback/before_tool_callback to each Agent individually.
    runner = Runner(
        agent=agent,
        app_name=APP_NAME,
        session_service=session_service,
        plugins=GUARDRAIL_PLUGINS,
    )

    async for _event in runner.run_async(
        user_id=user_id,
        session_id=session_id,
        new_message=types.Content(
            role="user", parts=[types.Part.from_text(text=kickoff_text)]
        ),
    ):
        pass  # drain to completion — the result we want is final state, not events

    session = await session_service.get_session(
        app_name=APP_NAME, user_id=user_id, session_id=session_id
    )
    return dict(session.state)


def run_agent_sync(
    agent: BaseAgent, initial_state: dict, kickoff_text: str = "Begin."
) -> dict:
    """Runs `agent` (a single LlmAgent or a composite like SequentialAgent)
    to completion in a fresh in-memory session seeded with `initial_state`,
    and returns the final session state as a plain dict. Raises on any
    failure — network, malformed model output against output_schema,
    anything — same contract as the call_gemini_json() this replaces:
    callers decide whether/how to fall back, this function never does."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(_run_agent_async(agent, initial_state, kickoff_text))

    # a loop is already running on this thread (e.g. called from a tool
    # inside ADK's own Runner) — asyncio.run() would raise; run the whole
    # bridge on a fresh thread with its own loop instead.
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            asyncio.run, _run_agent_async(agent, initial_state, kickoff_text)
        )
        return future.result()
