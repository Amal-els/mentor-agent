---
name: friday-review
description: Write the Friday reflection — what got done this week, what slipped and why, and one adjustment for next week, drawn from the accomplishment ledger.
feature: F4
intent: pulse
reads: [Commitment, WorkItem, Goal, Event]
writes: [Card]
model: composition
---

# friday-review — evidence first, then one adjustment

The review is worth reading only if every win is checkable. It is a mirror,
not a performance report, and never a scorecard.

## Card shape
1. **Shipped** — completed items, each citing the source event (merged PR,
   closed ticket, delivered promise). No citation ⇒ it does not appear.
2. **Moved the goal** — the subset that maps to an active OKR/Goal, with which one.
3. **Slipped** — open commitments past their expected date, with the blocker if
   one is recorded. State the fact; do not diagnose the person.
4. **One adjustment** — a single concrete change for next week, derived from the
   slipped section. One. Not a list of improvements.

## Rules
- Ledger entries are logged silently during the week; the review reads them,
  never invents them.
- Private, always. This data is the most sensitive the agent holds — it is never
  surfaced to a manager without an explicit grant record (§6.2).
- No trend claims from fewer than 3 weeks of data.
- Neutral voice: no praise inflation, no scolding. The evidence carries the tone.

## Edge cases
| Situation | Do |
|---|---|
| Quiet week, little shipped | Say it was a quiet week, show what exists, skip the adjustment. |
| Same item slipped 3 weeks running | Name the pattern once, plainly, and suggest renegotiating the commitment. |
| A week off / on leave | Skip the review entirely. Do not review a vacation. |
| Ledger empty (new user) | Explain what the ledger will collect and ask for nothing else. |
| User disputes a listed win | Remove it and record the correction; the ledger is append-only with corrections. |
| Goals not connected | Ship Shipped + Slipped, say the goal mapping is unavailable. |
