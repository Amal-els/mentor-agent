"""L7 delivery: an audio version of the pulse card's Focus section
(user-requested — "audio version of the pulse card in Slack"), posted as a
threaded reply alongside the existing text card, never replacing it.

Three independent pieces:
- build_pulse_speech_script (pure, no network) — the deterministic
  fallback, a template concatenation of the card's own fields. Kept for
  when the LLM draft is unavailable, same "always have a safe non-LLM
  path" contract as fallback_rank/fallback_render.
- draft_audio_script (real LLM call, pulse_audio_script.v1) — the primary
  path: a genuinely comprehensive, natural-sounding narration rather than
  a literal field-by-field reading of the card, grounded only in the
  card's own data (same "never trust the LLM with facts it could invent"
  discipline as ranker/writer, enforced here by the prompt rather than a
  closed-set code check since there's no id list to validate against).
- synthesize_speech — the Cloud TTS call. Isolated so a TTS outage
  degrades to "no audio" rather than breaking the text card delivery that
  already works — same "enhancement, never a dependency" contract as the
  rest of L7 (e.g. Slack permalink lookups)."""

import json
import logging
from pathlib import Path

from google.adk.agents import Agent
from google.genai import types as genai_types
from pydantic import BaseModel

from app.core.adk_runner import run_agent_sync

logger = logging.getLogger(__name__)

# Neural2 instead of Standard — Standard voices are flat/monotone
# text-to-speech; Neural2 is Google's natural-prosody tier and actually
# sounds like a person with inflection, not a phone menu. Swapping for a
# different voice/locale is still a one-line change here, not a redesign.
_VOICE_LANGUAGE_CODE = "en-US"
_VOICE_NAME = "en-US-Neural2-F"
# Slightly faster and a touch brighter than the API default (1.0/0.0) —
# reads as energetic rather than a flat recitation, without tipping into
# sounding rushed or cartoonish.
_VOICE_SPEAKING_RATE = 1.08
_VOICE_PITCH = 1.5

NARRATOR_MODEL = "gemini-3.5-flash-lite"
NARRATOR_PROMPT_ID = "pulse_audio_script.v1"
_NARRATOR_PROMPT_PATH = (
    Path(__file__).resolve().parents[1] / "prompts" / "pulse_audio_script.v1.md"
)


class NarratorOutput(BaseModel):
    script: str


narrator_agent = Agent(
    name="pulse_narrator",
    model=NARRATOR_MODEL,
    instruction=_NARRATOR_PROMPT_PATH.read_text(encoding="utf-8")
    + "\n\nPulse data (JSON):\n{narrator_input_json}",
    output_schema=NarratorOutput,
    output_key="narrator_result",
    # Some warmth for natural phrasing — unlike ranker/writer, which are
    # pinned to 0 because their output must be byte-identical for the same
    # input; a spoken script has no such requirement, only the grounding
    # rule (enforced by the prompt, not temperature).
    generate_content_config=genai_types.GenerateContentConfig(temperature=0.4),
)


def build_pulse_speech_script(card) -> str:
    """Spoken-friendly text for the card's Focus section — not a literal
    reading of render_text(card): no markdown, no urls, no footer/prompt
    ids/context hash (meaningless spoken), and "why now"/"action" said in
    plain sentences rather than left as labelled fields. Day/Owed are
    deliberately left out — Focus is the part someone actually wants read
    to them; the rest is still on the text card for when they look.

    This is the deterministic fallback only — draft_audio_script below is
    the primary path when an LLM call is available."""
    if not card.focus:
        return "Good morning. Nothing cleared the bar today."

    lines = ["Good morning. Here's your focus for today."]
    count_word = "one thing" if len(card.focus) == 1 else f"{len(card.focus)} things"
    lines.append(f"You've got {count_word}.")

    for i, item in enumerate(card.focus, start=1):
        lines.append(f"Number {i}: {item.title}.")
        if item.why_now:
            lines.append(item.why_now.rstrip(".") + ".")
        if item.action:
            lines.append(f"Next step: {item.action.rstrip('.')}.")

    if card.suggested_focus:
        lines.append(f"If you only do one thing, make it: {card.suggested_focus}")

    return " ".join(lines)


def _narrator_input(card) -> dict:
    return {
        "focus": [
            {
                "title": item.title,
                "why_now": item.why_now,
                "action": item.action,
                "detail": item.detail,
            }
            for item in card.focus
        ],
        "day_count": len(card.day),
        "owed_count": len(card.owed),
        "suggested_focus": card.suggested_focus,
        "degradation_line": card.degradation_line,
    }


def draft_audio_script(card) -> str | None:
    """The primary script source — returns None on any failure (missing
    credentials, API error, malformed output), same degrade-gracefully
    contract as synthesize_speech; callers fall back to
    build_pulse_speech_script. Never raises."""
    try:
        state = run_agent_sync(
            narrator_agent,
            {"narrator_input_json": json.dumps(_narrator_input(card))},
        )
        return NarratorOutput.model_validate(state["narrator_result"]).script
    except Exception:
        logger.exception("pulse tts: LLM script draft failed, falling back to template")
        return None


def deliver_pulse_audio(deliverer, card, channel_id: str, thread_ts: str) -> None:
    """Best-effort companion to a just-delivered text card — call after
    SlackDeliverer.deliver() has already succeeded, threaded under that
    same message (thread_ts = its dm_ts). Never raises: a TTS or upload
    failure here must not retroactively affect a text delivery that
    already happened."""
    if not deliverer.enabled:
        return
    script = draft_audio_script(card) or build_pulse_speech_script(card)
    audio = synthesize_speech(script)
    if audio is None:
        return
    try:
        deliverer.upload_audio(
            channel_id,
            audio_bytes=audio,
            filename="pulse.mp3",
            title="Your morning pulse (audio)",
            thread_ts=thread_ts,
        )
    except Exception:
        logger.exception("pulse tts: upload failed")


DOSSIER_NARRATOR_MODEL = "gemini-3.5-flash-lite"
DOSSIER_NARRATOR_PROMPT_ID = "dossier_audio_script.v1"
_DOSSIER_NARRATOR_PROMPT_PATH = (
    Path(__file__).resolve().parents[1] / "prompts" / "dossier_audio_script.v1.md"
)


class DossierNarratorOutput(BaseModel):
    script: str


dossier_narrator_agent = Agent(
    name="dossier_narrator",
    model=DOSSIER_NARRATOR_MODEL,
    instruction=_DOSSIER_NARRATOR_PROMPT_PATH.read_text(encoding="utf-8")
    + "\n\nDossier data (JSON):\n{dossier_narrator_input_json}",
    output_schema=DossierNarratorOutput,
    output_key="dossier_narrator_result",
    generate_content_config=genai_types.GenerateContentConfig(temperature=0.4),
)


def build_dossier_speech_script(card, event_title: str | None = None) -> str:
    """Spoken-friendly text for a DossierCard — deterministic fallback
    only, same role as build_pulse_speech_script above: a template
    concatenation of the card's own fields for when the LLM draft is
    unavailable. No markdown, no urls, no source links (meaningless
    spoken) — those stay on the text card."""
    if card.nothing_to_prep:
        return "Nothing to prep for this one — no real signal to brief you on."

    who_text = ", ".join(card.who)
    opener = (
        f"Let's get prepared for {event_title}, your meeting with {who_text}."
        if event_title
        else f"Let's get prepared. You're meeting with {who_text}."
    )
    lines = [opener]
    if card.why_now:
        lines.append(card.why_now.rstrip(".") + ".")

    if card.talking_points:
        count_word = (
            "one thing"
            if len(card.talking_points) == 1
            else f"{len(card.talking_points)} things"
        )
        lines.append(f"There's {count_word} worth raising.")
        for i, point in enumerate(card.talking_points, start=1):
            lines.append(f"Number {i}: {point.text.rstrip('.')}.")

    if card.promised_and_not_delivered:
        lines.append(
            "Also on the table: " + "; ".join(card.promised_and_not_delivered) + "."
        )

    if card.blockers:
        lines.append("Worth checking in on: " + "; ".join(card.blockers) + ".")

    if card.suggested_opener:
        lines.append(f"To open: {card.suggested_opener}")

    return " ".join(lines)


def _dossier_narrator_input(card, event_title: str | None = None) -> dict:
    return {
        "event_title": event_title,
        "who": card.who,
        "why_now": card.why_now,
        "talking_points": [{"text": p.text} for p in card.talking_points],
        "promised_and_not_delivered": card.promised_and_not_delivered,
        "blockers": card.blockers,
        "suggested_opener": card.suggested_opener,
        "nothing_to_prep": card.nothing_to_prep,
    }


def draft_dossier_audio_script(card, event_title: str | None = None) -> str | None:
    """The primary script source for a dossier's audio companion —
    returns None on any failure, same degrade-gracefully contract as
    draft_audio_script; callers fall back to build_dossier_speech_script.
    Never raises."""
    try:
        state = run_agent_sync(
            dossier_narrator_agent,
            {
                "dossier_narrator_input_json": json.dumps(
                    _dossier_narrator_input(card, event_title)
                )
            },
        )
        return DossierNarratorOutput.model_validate(
            state["dossier_narrator_result"]
        ).script
    except Exception:
        logger.exception(
            "dossier tts: LLM script draft failed, falling back to template"
        )
        return None


def deliver_dossier_audio(
    deliverer, card, channel_id: str, thread_ts: str, event_title: str | None = None
) -> None:
    """Best-effort companion to a just-delivered dossier text card — call
    after SlackDeliverer.deliver() has already succeeded, threaded under
    that same message. Never raises: a TTS or upload failure here must
    not retroactively affect a text delivery that already happened."""
    if not deliverer.enabled:
        return
    script = draft_dossier_audio_script(card, event_title) or build_dossier_speech_script(
        card, event_title
    )
    audio = synthesize_speech(script)
    if audio is None:
        return
    try:
        deliverer.upload_audio(
            channel_id,
            audio_bytes=audio,
            filename="dossier.mp3",
            title="Your pre-meeting dossier (audio)",
            thread_ts=thread_ts,
        )
    except Exception:
        logger.exception("dossier tts: upload failed")


def synthesize_speech(text: str) -> bytes | None:
    """Returns MP3 bytes, or None on any failure (missing credentials, API
    error, quota) — callers treat that as "skip the audio", the same
    degrade-gracefully contract as everywhere else in this codebase (e.g.
    app/ingest/seed.py's _fetch_or_degrade). Never raises."""
    try:
        from google.cloud import texttospeech

        client = texttospeech.TextToSpeechClient()
        response = client.synthesize_speech(
            input=texttospeech.SynthesisInput(text=text),
            voice=texttospeech.VoiceSelectionParams(
                language_code=_VOICE_LANGUAGE_CODE, name=_VOICE_NAME
            ),
            audio_config=texttospeech.AudioConfig(
                audio_encoding=texttospeech.AudioEncoding.MP3,
                speaking_rate=_VOICE_SPEAKING_RATE,
                pitch=_VOICE_PITCH,
            ),
        )
        return response.audio_content
    except Exception:
        logger.exception("pulse tts: synthesis failed, skipping audio")
        return None


FRIDAY_REVIEW_NARRATOR_MODEL = "gemini-3.5-flash-lite"
FRIDAY_REVIEW_NARRATOR_PROMPT_ID = "friday_review_audio_script.v1"
_FRIDAY_REVIEW_NARRATOR_PROMPT_PATH = (
    Path(__file__).resolve().parents[1] / "prompts" / "friday_review_audio_script.v1.md"
)


class FridayReviewNarratorOutput(BaseModel):
    script: str


friday_review_narrator_agent = Agent(
    name="friday_review_narrator",
    model=FRIDAY_REVIEW_NARRATOR_MODEL,
    instruction=_FRIDAY_REVIEW_NARRATOR_PROMPT_PATH.read_text(encoding="utf-8")
    + "\n\nFriday review data (JSON):\n{friday_review_narrator_input_json}",
    output_schema=FridayReviewNarratorOutput,
    output_key="friday_review_narrator_result",
    generate_content_config=genai_types.GenerateContentConfig(temperature=0.4),
)


def build_friday_review_speech_script(card) -> str:
    """Deterministic fallback — a template concatenation of the card's own
    fields, same role as build_dossier_speech_script."""
    if card.quiet_week:
        return "It was a quiet week — not much to report, but here's what's real."

    lines = ["Here's your Friday reflection."]
    if card.wins:
        lines.append("This week: " + "; ".join(w.text for w in card.wins) + ".")
    if card.slipped_lines:
        lines.append("A few things slipped: " + "; ".join(card.slipped_lines) + ".")
    if card.one_adjustment:
        lines.append(f"One thing worth trying next week: {card.one_adjustment}")
    if card.career_narrative:
        lines.append(card.career_narrative)

    return " ".join(lines)


def _friday_review_narrator_input(card) -> dict:
    return {
        "wins": [w.text for w in card.wins],
        "slipped_lines": card.slipped_lines,
        "one_adjustment": card.one_adjustment,
        "agenda_resolved_lines": card.agenda_resolved_lines,
        "agenda_stuck_lines": card.agenda_stuck_lines,
        "okr_progress_lines": card.okr_progress_lines,
        "career_narrative": card.career_narrative,
        "skill_distribution_summary": card.skill_distribution_summary,
        "quiet_week": card.quiet_week,
    }


def draft_friday_review_audio_script(card) -> str | None:
    """The primary script source — returns None on any failure, same
    degrade-gracefully contract as draft_audio_script; callers fall back
    to build_friday_review_speech_script. Never raises."""
    try:
        state = run_agent_sync(
            friday_review_narrator_agent,
            {"friday_review_narrator_input_json": json.dumps(_friday_review_narrator_input(card))},
        )
        return FridayReviewNarratorOutput.model_validate(
            state["friday_review_narrator_result"]
        ).script
    except Exception:
        logger.exception(
            "friday review tts: LLM script draft failed, falling back to template"
        )
        return None


def deliver_friday_review_audio(deliverer, card, channel_id: str, thread_ts: str) -> None:
    """Best-effort companion to a just-delivered Friday review text card —
    call after SlackDeliverer.deliver() has already succeeded, threaded
    under that same message. Never raises: a TTS or upload failure here
    must not retroactively affect a text delivery that already happened."""
    if not deliverer.enabled:
        return
    script = draft_friday_review_audio_script(card) or build_friday_review_speech_script(card)
    audio = synthesize_speech(script)
    if audio is None:
        return
    try:
        deliverer.upload_audio(
            channel_id, audio_bytes=audio, filename="friday_review.mp3",
            title="Your Friday reflection (audio)", thread_ts=thread_ts,
        )
    except Exception:
        logger.exception("friday review tts: upload failed")
