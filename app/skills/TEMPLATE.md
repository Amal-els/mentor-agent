---
name: skill-name
description: One or two sentences in the words a user would actually type, because this string is also the router's keyword bag and the classifier's option text.
feature: F0
intent: matching-intent-name        # or `none` for always-loaded knowledge
reads: [Event, WorkItem, Goal, Commitment]   # L3 canonical types only
writes: [Card]                      # Card | Commitment | AgendaItem | none
model: composition                  # composition | none  (never for L1-L4 logic)
---

# skill-name — one-line purpose

What good output looks like, in two sentences. Judgment, not plumbing.

## Card shape
Fixed section order, with the rule for each section. Order is stable so it
becomes a habit for the reader.

## Rules
- Provenance: every claim traces to a normalized record.
- Degrade honestly: name the missing source in one line, then brief on the rest.
- Never invent a name, a date, or a commitment.

## Edge cases
| Situation | Do |
|---|---|
| ... | ... |
| ... | ... |
| ... | ... |
