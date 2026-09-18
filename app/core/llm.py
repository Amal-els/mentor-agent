"""Whether an LLM credential is configured — app/pipeline/pulse.py's gate
for even attempting the ranker/writer/critic ADK agents (app/sub_agents/
pulse/) at all, rather than going straight to the deterministic
fallbacks."""

import os


def creds_available() -> bool:
    """True iff a Gemini/Vertex credential is configured in the
    environment. Callers use this to decide whether even to attempt the LLM
    path — never attempt it wholly on faith and let the resulting exception
    quietly become 'the fallback fired' with no distinction from a genuine
    model failure. Two independent paths, matching .env.example: a Gemini
    API key (Google AI Studio), or Vertex AI (GOOGLE_GENAI_USE_VERTEXAI=true
    + GOOGLE_CLOUD_PROJECT — application-default credentials themselves
    aren't checked here since gcloud manages those outside the process
    environment)."""
    if os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY"):
        return True
    vertex_enabled = os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "").lower() == "true"
    return bool(vertex_enabled and os.environ.get("GOOGLE_CLOUD_PROJECT"))
