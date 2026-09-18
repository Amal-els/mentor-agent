---
name: prep-meeting
command: /mentor prep
aliases: [prep, prep me, brief me on, dossier, before my meeting, who am I meeting, prépare, réunion]
kind: ritual
feature: F1
layer_entry: 4          # needs identity resolution before composition
visibility: private     # contains notes about other people — never channel-visible
slots:
  - name: event
    type: event_ref
    resolver: identity+calendar
    default: next qualifying event in the next 8h
model: composition      # dossier-writer prompt, versioned
tier_hint: B
---

# prep-meeting — Pre-meeting dossier

Make the user walk in like they remember everything.

## Steps
1. Resolve the event. If the user named a person, not an event, resolve the person
   through L4 and take their next shared event.
2. Resolve **every attendee** to a `person_id`. Unresolved attendees stay
   unresolved and are listed as such — never guessed, never invented (§4).
3. Pull what memory already knows about each attendee first: past conversations,
   stated preferences, decisions taken, and **anything promised and not yet
   delivered** in either direction. Memory before web. Internal before external.
4. Only then enrich from work systems (Linear/Jira items you share, recent docs).
5. Compose the card in four parts, in this order:
   - **When** — time, duration, where, recurring or one-off.
   - **Who** — one line per attendee: role, last interaction, open items with them.
   - **Why now** — what this meeting is for, in the user's own past words if available.
   - **Three talking points** — specific, not generic. "Ask how the Berlin launch
     went" beats "discuss recent developments". If you can't be specific, give two.

## Rules
- A dossier is private by construction. In a channel: DM it and acknowledge publicly.
- Every fact carries provenance; anything inferred is marked as inferred.
- Never include ledger or career content about a *third* person (§6.1).
- 15-min-before push and this pull path share one handler; only `trigger` differs.

## Edge cases
| Situation | Do |
|---|---|
| No matching event | Say so, then offer the next qualifying event by name. Do not brief on nothing. |
| Several events match | List up to 3 with times and ask which — this is the one place a question beats a guess. |
| Memory knows nothing about an attendee | Say "first recorded interaction" explicitly, then give externally verifiable basics only. |
| 20 attendees (all-hands) | Do not enumerate. Brief the organizer + anyone with an open item, note the rest as a count. |
| Meeting starts in 2 minutes | Ship the short form: When + Who + one talking point. Late and complete is worse than fast and true. |
| Attendee data is out of L2 permission scope | Omit and say the dossier is partial and why. |
| Event was suppressed by the user | Still show it — pull bypasses suppression (§5). |
