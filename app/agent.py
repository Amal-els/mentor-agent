# ruff: noqa
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import datetime
from zoneinfo import ZoneInfo

from google.adk.agents import Agent
from google.adk.apps import App
from google.adk.models import Gemini
from google.genai import types

from pathlib import Path

from app.core.guardrails import GUARDRAIL_PLUGINS
from app.tools.custom_tools import (
    get_friday_reflection,
    get_morning_pulse,
    get_pre_meeting_dossier,
)

soul_path = Path(__file__).parent / "SOUL.md"
soul_content = soul_path.read_text(encoding="utf-8") if soul_path.exists() else ""


MODEL = "gemini-3.5-flash-lite"


def get_weather(query: str) -> str:
    """Simulates a web search. Use it get information on weather.

    Args:
        query: A string containing the location to get weather information for.

    Returns:
        A string with the simulated weather information for the queried location.
    """
    if "sf" in query.lower() or "san francisco" in query.lower():
        return "It's 60 degrees and foggy."
    return "It's 90 degrees and sunny."


def get_current_time(query: str) -> str:
    """Simulates getting the current time for a city.

    Args:
        city: The name of the city to get the current time for.

    Returns:
        A string with the current time information.
    """
    if "sf" in query.lower() or "san francisco" in query.lower():
        tz_identifier = "America/Los_Angeles"
    else:
        return f"Sorry, I don't have timezone information for query: {query}."

    tz = ZoneInfo(tz_identifier)
    now = datetime.datetime.now(tz)
    return f"The current time for query {query} is {now.strftime('%Y-%m-%d %H:%M:%S %Z%z')}"


root_agent = Agent(
    name="root_agent",
    model=Gemini(
        model=MODEL,
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    instruction=f"{soul_content}\n\n"
    "When the user asks for their pulse, morning briefing, or what's "
    "happening today, call get_morning_pulse. When they ask to be "
    "prepped or briefed for an upcoming meeting, or who they're about to "
    "talk to, call get_pre_meeting_dossier. When they ask for their "
    "Friday reflection, weekly review, or how their week went, call "
    "get_friday_reflection. Every one of these always uses this "
    "session's own user — never ask for or accept a different user id, "
    "there is no delegation mechanism (each can only ever be for "
    "whoever is talking to you right now).",
    tools=[
        get_weather,
        get_current_time,
        get_morning_pulse,
        get_pre_meeting_dossier,
        get_friday_reflection,
    ],
)

app = App(
    root_agent=root_agent,
    name="app",
    # Single shared guardrail plugin (app/core/guardrails.py) — screens
    # every model call in and out for prompt injection/jailbreaks/safety
    # before it reaches the LLM or the user.
    plugins=GUARDRAIL_PLUGINS,
)
