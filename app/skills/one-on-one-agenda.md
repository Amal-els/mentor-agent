---
name: one-on-one-agenda
description: Maintain the rolling one-on-one agenda with a person — carry over what we left open last time, add what came up since, and keep it short enough to actually get through.
feature: F2
intent: prep-meeting
reads: [Event, Person, Commitment, WorkItem, Goal]
writes: [AgendaItem, Card]
model: composition
---

# one-on-one-agenda — the same document, every time

A 1-on-1 agenda is a rolling object, not a fresh generation. Its value is
continuity: the thing you said you'd discuss actually shows up again.

## Card shape
1. **Carried over** — items from last time still open. Always first, always
   labelled with how many sessions they've survived ("3rd time").
2. **New since** — items created since the last session, max 4, ranked.
3. **You owe / they owe** — open commitments between the two people.
4. **Optional** — anything that fits if there's time. Explicitly droppable.

## Rules
- Append and close; never regenerate. A closed item never reappears.
- An item survives 3 sessions ⇒ surface that fact. Repeated non-discussion is
  itself the signal worth naming.
- Growth and career items belong here only if the user put them here. Do not
  infer career anxiety from activity data.
- Keep it to what fits in the meeting length: ~1 item per 8 minutes.

## Edge cases
| Situation | Do |
|---|---|
| First ever 1-on-1 with this person | No carry-over section. Offer 3 opening questions instead. |
| Meeting was skipped | Carry everything, note "skipped last week", do not re-rank. |
| Agenda has grown past 8 items | Show top 5, say "3 held back", never silently truncate. |
| User edits an item | Their wording wins permanently; never rewrite it on the next render. |
| The other person is the user's report | Owed items in both directions, same tone. No manager-flavoured nudging. |
| Identity unresolved for the counterpart | Halt: no agenda without a `person_id`. Ask which person is meant. |
