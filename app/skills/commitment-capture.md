---
name: commitment-capture
description: Detect promises in messages and meetings — who owes what to whom by when — and log them to the accomplishment ledger silently, without pinging anyone.
feature: F5
intent: commitments
reads: [Message, Event, Person]
writes: [Commitment]
model: composition
---

# commitment-capture — log quietly, ask never

Capture is the quietest thing the agent does. It writes to the ledger and
produces no notification. Precision matters far more than recall: a wrong
commitment erodes trust in every other feature.

## What counts
A commitment needs three parts: an **owner** (resolved `person_id`), an
**obligation** ("send the deck"), and a **time reference** (explicit or
"by end of week"). Missing owner or obligation ⇒ not a commitment.

## Confidence
- **High** — first person, explicit verb, explicit time ("I'll send it Friday").
  Log as `confirmed`.
- **Medium** — explicit verb, vague time ("I'll get to that"). Log as `soft`,
  no due date, never surfaced as "overdue".
- **Low** — hypothetical, conditional, or third-person hearsay. Do not log.

## Rules
- Silent by default. Capture never sends a card; the ledger surfaces later via
  `friday-review`, `meeting-prep`, and `/mentor commitments`.
- Never log against another person from a message the user merely observed.
- Store the verbatim source span plus its link. Every ledger row is auditable.
- Unresolved identity ⇒ do not log. There is no free-text owner field.
- Closing is evidence-based (delivery, merge, explicit "done"), or the user
  closes it by hand. The agent never marks something done by assumption.

## Edge cases
| Situation | Do |
|---|---|
| "Someone should look at this" | No owner. Do not log. |
| Sarcasm or a joke promise | Low confidence. Do not log. |
| Same promise repeated in 3 channels | One commitment, three source spans. |
| Promise later retracted in the thread | Close as `withdrawn`, keep both spans. |
| Promise in a private DM the agent can read | Log it, visibility `private`; never expose it in a channel surface. |
| Language is French or Arabic | Same rules; store the original span untranslated. |
