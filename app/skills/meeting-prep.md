---
name: meeting-prep
description: Write the pre-meeting dossier before a call — who I'm meeting, why it matters now, three talking points, and anything I promised them and have not yet delivered. Also called meeting prep, briefing, or "prep me for my next meeting".
feature: F1
intent: prep-meeting
reads: [Event, Person, WorkItem, Commitment]
writes: [Card]
model: composition
---

# meeting-prep — walk in knowing one thing you didn't

A dossier that only restates the invite is noise. Earn the ping with the one
fact the user would have missed.

## Card shape
1. **When** — time, duration, location/link. One line.
2. **Who** — each attendee: resolved name, role, and the last real interaction
   ("last spoke 12 days ago, about the migration"). Unresolved attendee ⇒ show
   the raw handle and say it is unresolved. Never guess an identity.
3. **Why now** — one sentence: the thing that changed since last time.
4. **Three talking points** — exactly three, ranked. Each is a claim plus its
   source. If only two are real, ship two and say so.
5. **Promised and not yet delivered** — open commitments *with these attendees*,
   both directions. This is the section that saves the meeting.

## Rules
- Push path: this dossier is gated by L5 salience. Gated means the card may never
  be sent; it must still be complete when pulled via `/mentor prep`.
- Provenance on every talking point. No source, no point.
- Private by default: in a channel, DM it and say "sent you that in DM".
- Never speculate about a person's motives, mood, or performance.

## Edge cases
| Situation | Do |
|---|---|
| 12 attendees | Profile the organizer plus the 3 with the most shared history; count the rest. |
| External attendee, no internal history | Say so plainly and prep from the agenda + public context only. |
| Recurring standup | Suppressed by default; if pulled, one line of deltas, not a dossier. |
| Meeting in 4 minutes | Ship Who + Why now only, labelled "short version". Late and complete beats on-time and absent. |
| No agenda, no history | Say "nothing to prep" and offer the three questions worth asking. |
| Attendee is the user's manager | Structural floor: never suppress, never soften the owed section. |
