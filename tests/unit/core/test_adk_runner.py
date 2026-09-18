import asyncio

import pytest

from app.core import adk_runner
from app.core.adk_runner import run_agent_sync


def test_run_agent_sync_works_with_no_running_loop(monkeypatch):
    async def fake_run_agent_async(agent, initial_state, kickoff_text):
        return {"echo": initial_state}

    monkeypatch.setattr(adk_runner, "_run_agent_async", fake_run_agent_async)

    result = run_agent_sync(object(), {"foo": "bar"})

    assert result == {"echo": {"foo": "bar"}}


@pytest.mark.asyncio
async def test_run_agent_sync_works_when_called_from_inside_a_running_loop(
    monkeypatch,
):
    # a live run against root_agent (app/tools/custom_tools.py's get_morning_pulse,
    # invoked as an ADK tool) caught this for real: asyncio.run() inside
    # this function used to raise "cannot be called from a running event
    # loop" the moment it was invoked from a tool running inside ADK's own
    # Runner — silently falling back to the deterministic path rather than
    # crashing, but never actually attempting the LLM call. This proves
    # run_agent_sync works from exactly that context instead of merely not
    # crashing from it.
    assert asyncio.get_running_loop() is not None  # sanity: we are in a loop

    async def fake_run_agent_async(agent, initial_state, kickoff_text):
        return {"echo": initial_state}

    monkeypatch.setattr(adk_runner, "_run_agent_async", fake_run_agent_async)

    result = run_agent_sync(object(), {"foo": "bar"})

    assert result == {"echo": {"foo": "bar"}}


def test_run_agent_sync_propagates_exceptions_from_inside_a_running_loop(
    monkeypatch,
):
    async def fake_run_agent_async(agent, initial_state, kickoff_text):
        raise RuntimeError("boom")

    monkeypatch.setattr(adk_runner, "_run_agent_async", fake_run_agent_async)

    async def _inner():
        run_agent_sync(object(), {})

    try:
        asyncio.run(_inner())
        raised = False
    except RuntimeError as exc:
        raised = str(exc) == "boom"

    assert raised is True
