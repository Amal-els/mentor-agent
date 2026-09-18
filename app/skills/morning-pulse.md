---
name: morning-pulse
description: Compose the morning Command Center pulse — what actually matters today, my day, what I owe people, and one suggested focus. Also called the daily brief, morning brief, or "what's on today".
feature: F3
intent: pulse
reads: [Event, WorkItem, Goal, Commitment]
writes: [Card]
model: composition
---

# morning-pulse — the shortest true answer to "what matters today"

A pulse is a decision, not a dump. If the reader has to scan it, it failed.
Rank hard, cut to 1–3 focus items, group the rest at one line each.

## Card shape
1. **Focus** — 1–3 items. Each carries a *why now*: a deadline, a person
   waiting, or an OKR it moves. No why-now ⇒ it is not focus, it is Day.
2. **Day** — today's events, chronological, one line each. Flag the ones that
   have a dossier waiting (`meeting-prep`).
3. **Owed** — promised and not yet delivered, oldest first, max 3. Name the
   person and the promise, never the guilt.
4. **Suggested focus** — one imperative sentence. Pick something from Focus;
   never introduce a new item here.

## Rules
- Stable order, always. Readers learn the shape; changing it costs them a re-read.
- Degrade honestly. Missing or unauthorized source ⇒ one line naming it
  ("no calendar connected — Slack + Linear only"), then brief on what exists.
  Never quietly ship a thinner pulse.
- Determinism in P1: the ranker is code (L5). This skill only decides wording,
  section membership, and what to leave out.
- Same handler for cron and `/mentor pulse`; only `trigger` differs. Wording may
  acknowledge the trigger ("since 09:12"), nothing else may.

## Edge cases
| Situation | Do |
|---|---|
| Clear calendar, no open items | Say the day is clear in one line, then the single oldest owed item. Do not pad. |
| Second pulse the same day | Show deltas only, labelled "since HH:MM". |
| A source errored (not unauthorized) | Name it, mark "stale as of HH:MM", still deliver. |
| Everything looks urgent | Still cut to 3. Say "3 of 9 — ask for the rest" rather than widening. |
| Requested in a channel | DM or ephemeral; a pulse is private. |
| No focus item has a why-now | Ship Day + Owed and say focus is unclear. Never fabricate urgency. |
