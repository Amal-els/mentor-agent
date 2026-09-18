---
name: pulse
command: /mentor pulse
aliases: [pulse, morning, brief, today, what's on today, quoi aujourd'hui, ma journée]
kind: ritual
feature: F3
layer_entry: 5          # skips L1 scheduling, enters at salience
visibility: private     # ranked view of the user's own day
slots: []               # optional: date (defaults to today, user's tz)
model: composition      # pulse-ranker prompt, versioned
tier_hint: B            # keyword/description match should carry this
---

# pulse — Morning Command Center

Give the user the shortest true answer to "what actually matters today".

## Steps
1. Resolve the window: today 00:00–23:59 in the user's timezone (`core/clock.py`).
   Never `datetime.now()` directly.
2. Assemble context from L3 canonical types only: `Event`, `WorkItem`, `Goal`,
   plus open items from the commitment ledger (F5).
3. Rank with L5 scoring, then **cut to 1–3 focus items**. A pulse is a decision,
   not a dump. Everything else is grouped, one line each.
4. Compose in this order — and keep the order stable so it becomes a habit:
   - **Focus** — 1–3 items, each with *why now* (deadline, someone waiting, OKR).
   - **Day** — chronological events, one line, flagged if a dossier exists.
   - **Owed** — promised and not yet delivered (from the ledger), oldest first.
   - **Suggested focus for today** — one sentence, imperative.
5. Degrade honestly. If a source is missing or unauthorized, say which one in a
   single line ("no calendar connected — briefing on Slack + Linear only") and
   brief on what exists. Never silently produce a thinner pulse.

## Rules
- Pull bypasses the L5 suppression gate; it never bypasses L2 permissions.
- No item appears without provenance: every claim traces to a normalized record.
- Same context + same weights ⇒ same pulse (temperature pinned, prompt ID on card).
- The cron delivery of this ritual is the *same* handler; only `trigger` differs.

## Edge cases
| Situation | Do |
|---|---|
| Nothing on the calendar, no open items | Say the day is clear in one line, then surface the single oldest owed item. Do not pad. |
| Called twice in one day | Second call is fine (pull). Mark it "since 09:12" and show only deltas. |
| Called at 23:40 | Ask nothing — pulse tomorrow, and say so ("showing tomorrow, it's late"). |
| A source errored (not unauthorized) | Name the source, say "stale as of HH:MM", still deliver. |
| Requested in a channel | Reply ephemeral or DM; post "sent you that in DM". |
| Requested for someone else | Always refuse. No delegation mechanism exists — cross-user pulses are disabled entirely (decision 4, `docs/plans/morning-pulse.md`), not gated behind a grant record. |
