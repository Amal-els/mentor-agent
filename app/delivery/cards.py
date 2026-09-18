"""L7 delivery: renders a PulseCard from a TriggerResult (M5/M6). Fixed
Focus/Today's-meeting-load/Owed/Suggested-focus order, degradation line on
top, footer with prompt ids + context hash. Pure rendering — no DB, no
LLM, no network. Structured data first (PulseCard), then two independent
renderers so neither format can drift from what the pipeline actually
produced."""

import datetime
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

from app.pipeline.pulse import context_hash
from app.salience.pulse.config import PERSONALIZATION_NOTE_THRESHOLD, WEIGHT_NEUTRAL
from app.salience.pulse.types import DayEventSummary, OwedItem, ScoredItem
from app.sub_agents.dossier.sub_agents.synthesize.agent import DossierCard
from app.sub_agents.friday_review.sub_agents.synthesize.agent import FridayReviewCard
from app.sub_agents.pulse.sub_agents.writer.agent import PulseItem

# Human-readable label per item_type key in PulseContext.weights — used
# only by _personalization_note below, never fed to an LLM.
_ITEM_TYPE_LABELS = {
    "event": "events",
    "work_item": "work items",
    "message": "messages",
}

# Slack interactive block_actions action_id for the pulse card's "See all
# N items" button — app/triggers/slack_socket_listener.py matches on this
# to know which button was clicked (Slack sends every block_actions
# interaction through the same envelope type).
SHOW_FULL_SHORTLIST_ACTION_ID = "show_full_shortlist"

# Per-focus-item 👍/👎 — app/triggers/slack_socket_listener.py routes these
# to app/delivery/affordances.py's record_feedback, the instant half of
# AGENT.md L5's three-clock weight design (the nightly consolidation batch,
# app/salience/weight_consolidation.py, is the other half).
FEEDBACK_UP_ACTION_ID = "pulse_feedback_up"
FEEDBACK_DOWN_ACTION_ID = "pulse_feedback_down"
_FEEDBACK_VALUE_SEP = "::"


def _feedback_value(delivery_id: str, item_type: str, item_id: str) -> str:
    return _FEEDBACK_VALUE_SEP.join((delivery_id, item_type, item_id))


def parse_feedback_value(value: str) -> tuple[str, str, str]:
    """Inverse of _feedback_value — app/triggers/slack_socket_listener.py
    calls this on a feedback button's Slack-echoed value. item_id itself
    may contain the separator (uuids never do, but this stays correct
    either way by capping the split), so this splits into exactly 3 parts,
    not naively on every occurrence."""
    delivery_id, item_type, item_id = value.split(_FEEDBACK_VALUE_SEP, 2)
    return delivery_id, item_type, item_id


@dataclass(frozen=True)
class PulseCard:
    focus: list[PulseItem]
    day: list[DayEventSummary]
    owed: list[OwedItem]
    suggested_focus: str | None
    degradation_line: str | None
    since_note: str | None
    shown_count: int
    total_count: int
    footer: str
    # None for a dry_run's card (no PulseDelivery row exists to attribute
    # feedback to) — render_blocks omits the feedback buttons entirely in
    # that case rather than embedding an id that doesn't resolve to anything.
    delivery_id: str | None = None
    focus_item_types: dict[str, str] = field(default_factory=dict)
    personalization_note: str | None = None
    # item_id -> is_new_since_last_pulse, for focus items where the
    # underlying ScoredItem actually carries the term (work items/
    # messages only — events never do, so they're simply absent here,
    # not False). Deterministic, sourced straight from context.shortlist,
    # not from the writer's prose — the writer/critic prompts also ask
    # for this in why_now's own language, but that's an LLM best-effort,
    # not load-bearing: this dict is what the renderers actually key off
    # to guarantee the marker shows every time, LLM path or fallback.
    focus_item_novelty: dict[str, bool] = field(default_factory=dict)
    # item_type -> count of shortlist candidates that were real work
    # items/messages but didn't win a Focus slot — "event" excluded
    # (already fully shown in Today's meeting load, focus or not). See
    # build_card's own comment on why this is counts-only, no per-item
    # detail.
    also_happening_counts: dict[str, int] = field(default_factory=dict)


def _personalization_note(weights: dict[str, float]) -> str | None:
    """Deterministic, not LLM-authored — this is a plain description of a
    number the scorer already computed (app/salience/assemble.py's
    event_weight/work_item_weight/message_weight, get_effective_weight()),
    not a claim that needs grounding review. Picks whichever item_type has
    drifted furthest from WEIGHT_NEUTRAL and only speaks up past
    PERSONALIZATION_NOTE_THRESHOLD — a handful of clicks producing a 1-2%
    drift is noise, not a settled preference worth mentioning every day."""
    if not weights:
        return None
    item_type, weight = max(weights.items(), key=lambda kv: abs(kv[1] - WEIGHT_NEUTRAL))
    deviation = (weight - WEIGHT_NEUTRAL) / WEIGHT_NEUTRAL
    if abs(deviation) < PERSONALIZATION_NOTE_THRESHOLD:
        return None
    direction = "higher" if deviation > 0 else "lower"
    label = _ITEM_TYPE_LABELS.get(item_type, item_type)
    return (
        f"🧠 Weighting {label} {abs(deviation):.0%} {direction} than usual, "
        "based on your recent 👍/👎."
    )


def build_card(trigger_result) -> "PulseCard":
    rendered = trigger_result.rendered
    context = rendered.context
    # Postgres timestamptz round-trips everything tagged UTC regardless of
    # the offset used at insert time — every user-facing timestamp must be
    # converted to the owner's tz here, or the card silently shows the
    # wrong wall-clock time.
    tz = ZoneInfo(context.owner_tz) if context.owner_tz else datetime.UTC

    suggested_focus = rendered.items[0].action if rendered.items else None

    since_note = None
    if trigger_result.previous_delivered_at is not None:
        local_previous = trigger_result.previous_delivered_at.astimezone(tz)
        since_note = f"since {local_previous.strftime('%H:%M')}"

    day = [
        DayEventSummary(
            item_id=event.item_id,
            title=event.title,
            starts_at=event.starts_at.astimezone(tz),
            has_dossier=event.has_dossier,
            dossier_id=event.dossier_id,
            url=event.url,
        )
        for event in context.day_events
    ]

    ctx_hash = context_hash(
        [{"item_id": item.item_id, "score": item.score} for item in context.shortlist]
    )
    footer = (
        f"prompt_ids=[{rendered.ranker_prompt_id}, {rendered.writer_prompt_id}, "
        f"{rendered.critic_prompt_id}] context_hash={ctx_hash}"
    )

    item_types = {item.item_id: item.item_type for item in context.shortlist}
    focus_item_types = {
        item.item_id: item_types[item.item_id]
        for item in rendered.items
        if item.item_id in item_types
    }
    novelty_by_item_id = {
        item.item_id: item.score_terms["is_new_since_last_pulse"]
        for item in context.shortlist
        if "is_new_since_last_pulse" in item.score_terms
    }
    focus_item_novelty = {
        item.item_id: novelty_by_item_id[item.item_id]
        for item in rendered.items
        if item.item_id in novelty_by_item_id
    }

    # Work items/messages that were real candidates but didn't win a
    # Focus slot — the full shortlist already has them (context.shortlist,
    # uncapped by design), just never surfaced anywhere in the card
    # itself until now. "event" is deliberately excluded here: every
    # event, focus or not, already shows in Today's meeting load below —
    # counting them again here would just double them up. Counts only,
    # not per-item detail (title/who) — build_card is pure rendering, no
    # DB, and a Message row has no title to show at all (AGENT.md privacy
    # rule); "ask for the rest"/the Full shortlist button is still the
    # way to see what they actually are.
    focus_ids = {item.item_id for item in rendered.items}
    also_happening_counts: dict[str, int] = {}
    for item in context.shortlist:
        if item.item_id in focus_ids or item.item_type == "event":
            continue
        also_happening_counts[item.item_type] = (
            also_happening_counts.get(item.item_type, 0) + 1
        )

    return PulseCard(
        focus=rendered.items,
        day=day,
        owed=context.owed,
        suggested_focus=suggested_focus,
        degradation_line=rendered.degradation_line,
        since_note=since_note,
        shown_count=len(rendered.items),
        total_count=len(context.shortlist),
        footer=footer,
        delivery_id=trigger_result.delivery_id,
        focus_item_types=focus_item_types,
        focus_item_novelty=focus_item_novelty,
        also_happening_counts=also_happening_counts,
        personalization_note=_personalization_note(context.weights),
    )


def card_to_dict(card: PulseCard) -> dict:
    """JSON-serializable snapshot of a PulseCard — persisted onto
    PulseDelivery.card_json (see that column's own docstring) and, in the
    same shape, pushed as the pulse_ready SSE event's "card" field so the
    /ui agent bubble can render the real Focus items instead of a bare
    count. Deliberately flat/plain (no nested dataclasses, no Slack Block
    Kit) — this is a data contract with the browser, not a rendering
    format, so render_text/render_blocks stay the only two renderers with
    Slack-specific knowledge."""
    return {
        "focus": [
            {
                "item_id": item.item_id,
                "title": item.title,
                "why_now": item.why_now,
                "action": item.action,
                "url": item.url,
                "detail": item.detail,
                "novelty": card.focus_item_novelty.get(item.item_id),
            }
            for item in card.focus
        ],
        "day": [
            {
                "title": event.title,
                "starts_at": event.starts_at.isoformat(),
                "has_dossier": event.has_dossier,
                "dossier_id": event.dossier_id,
                "url": event.url,
            }
            for event in card.day
        ],
        "owed": [
            {
                "description": owed.description,
                "promised_to": owed.promised_to,
                "overdue": owed.overdue,
            }
            for owed in card.owed
        ],
        "suggested_focus": card.suggested_focus,
        "degradation_line": card.degradation_line,
        "since_note": card.since_note,
        "shown_count": card.shown_count,
        "total_count": card.total_count,
        "also_happening_counts": card.also_happening_counts,
        "personalization_note": card.personalization_note,
    }


def _also_happening_line(counts: dict[str, int]) -> str | None:
    if not counts:
        return None
    labels = {"work_item": "work item", "message": "message"}
    parts = []
    for item_type in ("message", "work_item"):
        count = counts.get(item_type, 0)
        if not count:
            continue
        label = labels.get(item_type, item_type)
        parts.append(f"{count} {label}{'s' if count != 1 else ''}")
    if not parts:
        return None
    return f"💬 Also happening: {', '.join(parts)} didn't make Focus — ask for the rest"


def render_text(card: PulseCard) -> str:
    lines: list[str] = []
    if card.degradation_line:
        lines.append(card.degradation_line)

    header = "Focus"
    if card.since_note:
        header += f" ({card.since_note})"
    lines.append(header)
    if not card.focus:
        lines.append("  (none — nothing cleared the bar today)")
    for item in card.focus:
        novelty = card.focus_item_novelty.get(item.item_id)
        marker = " 🆕" if novelty is True else (" 🔁" if novelty is False else "")
        lines.append(
            f"  {item.title}{marker}" + (f" ({item.url})" if item.url else "")
        )
        if item.detail:
            lines.append(f"    {item.detail}")
        lines.append(f"    why now: {item.why_now}")
        lines.append(f"    action: {item.action}")
    if card.total_count > card.shown_count:
        lines.append(f"  {card.shown_count} of {card.total_count} — ask for the rest")
    also_happening = _also_happening_line(card.also_happening_counts)
    if also_happening:
        lines.append(f"  {also_happening}")

    lines.append("")
    lines.append("Today's meeting load")
    if not card.day:
        lines.append("  (clear)")
    for event in card.day:
        flag = " [dossier]" if event.has_dossier else ""
        lines.append(f"  {event.starts_at.strftime('%H:%M')} {event.title}{flag}")

    lines.append("")
    lines.append("Owed")
    if not card.owed:
        lines.append("  (nothing owed)")
    for owed in card.owed:
        overdue = " (overdue)" if owed.overdue else ""
        lines.append(f"  {owed.description} — {owed.promised_to}{overdue}")

    lines.append("")
    lines.append("Suggested focus")
    lines.append(f"  {card.suggested_focus}" if card.suggested_focus else "  (unclear)")

    if card.personalization_note:
        lines.append("")
        lines.append(card.personalization_note)

    lines.append("")
    lines.append(card.footer)

    return "\n".join(lines)


_FOCUS_ORDINAL_EMOJI = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣"]


def _focus_ordinal(index: int) -> str:
    return (
        _FOCUS_ORDINAL_EMOJI[index]
        if index < len(_FOCUS_ORDINAL_EMOJI)
        else f"{index + 1}."
    )


def render_blocks(card: PulseCard) -> list[dict]:
    blocks: list[dict] = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "🌅 Morning Pulse", "emoji": True},
        }
    ]

    if card.degradation_line:
        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": f"⚠️ {card.degradation_line}"}],
            }
        )

    focus_header = "🎯 *Focus*"
    if card.since_note:
        focus_header += f" _{card.since_note}_"
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": focus_header}})
    if not card.focus:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "😌 _nothing cleared the bar today — enjoy the calm_",
                },
            }
        )
    for index, item in enumerate(card.focus):
        heading = f"*<{item.url}|{item.title}>*" if item.url else f"*{item.title}*"
        novelty = card.focus_item_novelty.get(item.item_id)
        if novelty is True:
            heading += " 🆕"
        elif novelty is False:
            heading += " 🔁"
        detail_line = f"_{item.detail}_\n" if item.detail else ""
        ordinal = _focus_ordinal(index)
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"{ordinal} {heading}\n{detail_line}{item.why_now}\n_{item.action}_",
                },
            }
        )
        item_type = card.focus_item_types.get(item.item_id)
        if card.delivery_id is not None and item_type is not None:
            value = _feedback_value(card.delivery_id, item_type, item.item_id)
            blocks.append(
                {
                    "type": "actions",
                    "block_id": f"pulse_feedback_{item.item_id}",
                    "elements": [
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "👍"},
                            "action_id": FEEDBACK_UP_ACTION_ID,
                            "value": value,
                        },
                        {
                            "type": "button",
                            "text": {"type": "plain_text", "text": "👎"},
                            "action_id": FEEDBACK_DOWN_ACTION_ID,
                            "value": value,
                        },
                    ],
                }
            )
    if card.total_count > card.shown_count:
        blocks.append(
            {
                "type": "actions",
                "block_id": "pulse_shortlist_actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {
                            "type": "plain_text",
                            "text": f"👀 See all {card.total_count} items",
                        },
                        "action_id": SHOW_FULL_SHORTLIST_ACTION_ID,
                    }
                ],
            }
        )
    also_happening = _also_happening_line(card.also_happening_counts)
    if also_happening:
        blocks.append(
            {"type": "context", "elements": [{"type": "mrkdwn", "text": also_happening}]}
        )

    blocks.append({"type": "divider"})
    blocks.append(
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "🗓️ *Today's meeting load*"},
        }
    )
    if not card.day:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "☀️ _clear — nothing on the calendar_",
                },
            }
        )
    for event in card.day:
        suffix = " :page_facing_up:" if event.has_dossier else ""
        title = f"<{event.url}|{event.title}>" if event.url else event.title
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"• {event.starts_at.strftime('%H:%M')} {title}{suffix}",
                },
            }
        )

    blocks.append({"type": "divider"})
    blocks.append({"type": "section", "text": {"type": "mrkdwn", "text": "🤝 *Owed*"}})
    if not card.owed:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "✅ _nothing owed — you're all caught up_",
                },
            }
        )
    for owed in card.owed:
        suffix = " 🔴 _overdue_" if owed.overdue else ""
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"• {owed.description} — {owed.promised_to}{suffix}",
                },
            }
        )

    blocks.append({"type": "divider"})
    blocks.append(
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    f"🔥 *If you do only one thing:*\n{card.suggested_focus}"
                    if card.suggested_focus
                    else "🔥 *If you do only one thing:*\n_unclear_"
                ),
            },
        }
    )
    if card.personalization_note:
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": f"_{card.personalization_note}_"}
                ],
            }
        )
    blocks.append(
        {"type": "context", "elements": [{"type": "mrkdwn", "text": card.footer}]}
    )

    return blocks


def _score_term_summary(item: ScoredItem) -> str:
    terms = item.score_terms
    bits = []
    if terms.get("overdue"):
        bits.append("overdue")
    elif terms.get("due_today"):
        bits.append("due today")
    person = terms.get("person_waiting")
    if person:
        bits.append(f"{person} waiting")
    return ", ".join(bits) if bits else "—"


def render_shortlist_blocks(
    shortlist: list[ScoredItem],
    titles: dict[str, str],
    urls: dict[str, str | None] | None = None,
) -> list[dict]:
    """The full pre-cut L5 shortlist (every candidate, not just the 1-3 the
    ranker chose for Focus) — the follow-up posted when the pulse card's
    "See all N items" button is clicked. Deliberately simpler than the
    pulse card itself: score + score_terms only, no why_now/action (those
    are the ranker/writer's job, never fabricated here).

    urls links the title itself straight to the source item — added after
    a live run had six same-scored, identically-titled "Slack mention"
    entries with no way to tell them apart or jump to any of them."""
    urls = urls or {}
    blocks: list[dict] = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Full shortlist* ({len(shortlist)} items)",
            },
        }
    ]
    if not shortlist:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "_nothing in today's shortlist_"},
            }
        )
        return blocks

    for item in sorted(shortlist, key=lambda i: i.score, reverse=True):
        title = titles.get(item.item_id, item.item_id)
        url = urls.get(item.item_id)
        heading = f"*<{url}|{title}>*" if url else f"*{title}*"
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"{heading}\nscore={item.score:.2f} · {_score_term_summary(item)}",
                },
            }
        )
    return blocks


def build_agenda_summary_blocks(items: list[dict]) -> list[dict]:
    """Converts a list of agenda item dicts (app.sub_agents.agenda.
    sub_agents.deliver.agent._item_to_dict shape: id/text/source/
    visibility/status) into Slack Block Kit blocks for post-meeting DM
    delivery. Takes a flat item list, not build_a2ui_payload's
    {"components": [...]} shape — that shape is UI-component-oriented
    (paired editable_text/visibility_toggle entries keyed by item_id,
    no source_link) and was never meant for this kind of flattened
    rendering. Callers are expected to have already filtered items to
    the specific recipient's visibility — this function renders whatever
    it's given without re-checking visibility itself."""
    blocks: list[dict] = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": "📋 Post-Meeting 1-on-1 Agenda Summary",
                "emoji": True,
            },
        },
        {"type": "divider"},
    ]

    if items:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "*Agenda Items:*"},
            }
        )
        for item in items:
            text = item.get("text", "")
            source = item.get("source", "manual")
            vis = item.get("visibility", "shared")
            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": f"• {text} _[{source} | {vis}]_",
                    },
                }
            )
    else:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "_No active agenda items._"},
            }
        )

    return blocks


def _format_dossier_event_time(starts_at: str) -> str:
    """Best-effort human time for the card header. starts_at is whatever
    ISO string the event dict carries (naive or offset-aware, per
    _adapt_calendar_event) — this only needs to be readable, not localized
    to the owner's own timezone the way pulse's Day section is.

    Builds the string field-by-field rather than via strftime's %-d/%-I
    (no leading-zero suppression) — those are glibc/macOS-only directives
    and raise on Windows' strftime, which this dev environment runs on."""
    try:
        parsed = datetime.datetime.fromisoformat(starts_at)
    except (TypeError, ValueError):
        return starts_at
    weekday_month_day = parsed.strftime("%a, %b") + f" {parsed.day}"
    hour_12 = parsed.hour % 12 or 12
    time_part = f"{hour_12}:{parsed.strftime('%M %p')}"
    tz_part = parsed.strftime("%Z").strip()
    return f"{weekday_month_day} · {time_part}" + (f" {tz_part}" if tz_part else "")


def build_dossier_card(
    card: DossierCard,
    event_title: str | None = None,
    event_starts_at: str | None = None,
) -> list[dict]:
    """Slack Block Kit blocks for a dossier. Same render pattern as
    build_card (pulse), new function — build_card is PulseCard-specific
    (spec §4's correction), not a generic builder.

    event_title/event_starts_at are optional so this stays usable with
    just a DossierCard (e.g. in tests that don't care which meeting it
    is) — but every real delivery (deliver_dossier) passes both, so the
    reader always sees which meeting this is prepping for, not just its
    content."""
    blocks: list[dict] = []
    if event_title:
        header_text = f"📋 *Dossier — {event_title}*"
        if event_starts_at:
            header_text += f"\n🕒 {_format_dossier_event_time(event_starts_at)}"
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": header_text}}
        )
        blocks.append({"type": "divider"})
    blocks.append(
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Who:* {', '.join(card.who)}"},
        }
    )
    blocks.append(
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Why now:* {card.why_now}"},
        }
    )
    if card.nothing_to_prep:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "_Nothing to prep._"},
            }
        )
        return blocks
    if card.talking_points:
        # AgendaItem.source_link is overloaded (app/agenda/store.py's
        # append_ledger_item sets it to the mirrored Commitment/
        # Accomplishment row's own internal id for ledger-sourced items,
        # not a clickable URL — same fact app.sub_agents.dossier.sub_
        # agents.synthesize.agent's drop_unsourced_talking_points already
        # had to account for). synthesize_dossier keeps these agenda-
        # carryover points (correctly — the text is real, DB-backed data)
        # but never guaranteed source_link was itself a real link; found
        # live rendering a literal broken "(<3311b5bc-...|source>)" in
        # Slack. Only linkify when it's an actual http(s) URL a person
        # could click — otherwise render the point as plain text, same
        # no-link treatment promised_and_not_delivered already gets below.
        lines = "\n".join(
            f"• {p.text} (<{p.source_link}|source>)"
            if p.source_link and p.source_link.startswith(("http://", "https://"))
            else f"• {p.text}"
            for p in card.talking_points
        )
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Talking points:*\n{lines}"},
            }
        )
    if card.promised_and_not_delivered:
        lines = "\n".join(f"• {item}" for item in card.promised_and_not_delivered)
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Promised, not delivered:*\n{lines}",
                },
            }
        )
    if card.blockers:
        lines = "\n".join(f"• {item}" for item in card.blockers)
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"🚧 *Blockers:*\n{lines}"},
            }
        )
    if card.suggested_opener:
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {"type": "mrkdwn", "text": f"Opener: {card.suggested_opener}"}
                ],
            }
        )
    return blocks


FRIDAY_REVIEW_CONFIRM_LOG_ACTION_ID = "friday_review_confirm_log"


def build_friday_review_card(
    card: FridayReviewCard, delivery_id: str, proposed_ledger_items: list[dict]
) -> list[dict]:
    """Slack Block Kit blocks for the Friday reflection. New function, own
    render pattern — same "one builder per ritual" precedent as
    build_dossier_card/build_card, not a generic composer."""
    blocks: list[dict] = [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Friday Reflection — week of {card.week_start.isoformat()}*",
            },
        },
        {"type": "divider"},
    ]

    if card.quiet_week:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "_Quiet week — not much to show, but here's what's real._",
                },
            }
        )

    if card.wins:
        lines = []
        for w in card.wins:
            suffix = f" (<{w.source_link}|source>)" if w.source_link else ""
            goal = f" — moved *{w.moved_goal_title}*" if w.moved_goal_title else ""
            lines.append(f"• {w.text}{suffix}{goal}")
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": "*Shipped:*\n" + "\n".join(lines)}}
        )

    if card.slipped_lines:
        lines = "\n".join(f"• {line}" for line in card.slipped_lines)
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": f"*Slipped:*\n{lines}"}}
        )

    if card.one_adjustment and not card.quiet_week:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*One adjustment:* {card.one_adjustment}"},
            }
        )

    if card.agenda_resolved_lines or card.agenda_stuck_lines:
        lines = [f"✅ {line}" for line in card.agenda_resolved_lines]
        lines += [f"⏳ {line} (worth raising directly)" for line in card.agenda_stuck_lines]
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "*Agenda 1-on-1s:*\n" + "\n".join(lines)},
            }
        )

    if card.okr_progress_lines:
        lines = "\n".join(f"• {line}" for line in card.okr_progress_lines)
        blocks.append(
            {"type": "section", "text": {"type": "mrkdwn", "text": f"*OKR progress:*\n{lines}"}}
        )

    if card.career_narrative:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Toward your goal:* {card.career_narrative}"},
            }
        )

    if card.next_week_focus_lines:
        lines = "\n".join(f"• {line}" for line in card.next_week_focus_lines)
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Focus for next week:*\n{lines}"},
            }
        )

    skill_text = card.skill_distribution_summary or "Not enough categorized history yet."
    blocks.append(
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Skill distribution:* {skill_text}"}}
    )

    if card.daily_pulse_patterns:
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": "Pattern from your daily briefs: "
                        + ", ".join(card.daily_pulse_patterns),
                    }
                ],
            }
        )

    if card.identity_asks_count > 0:
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"{card.identity_asks_count} \"who is this\" question(s) below.",
                    }
                ],
            }
        )

    if proposed_ledger_items:
        lines = "\n".join(f"• {item['description']}" for item in proposed_ledger_items)
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Log these {len(proposed_ledger_items)} to your ledger?*\n{lines}",
                },
            }
        )
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Confirm & log"},
                        "action_id": FRIDAY_REVIEW_CONFIRM_LOG_ACTION_ID,
                        "value": delivery_id,
                    }
                ],
            }
        )

    return blocks
