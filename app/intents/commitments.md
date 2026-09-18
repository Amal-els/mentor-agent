---
name: commitments
command: /mentor commitments
aliases: [commitments, what did I promise, owed, follow ups, open loops, ledger, mes engagements, promis]
kind: query
feature: F5
layer_entry: 4
visibility: private     # user-owned ledger, no admin path (§6.1)
slots:
  - name: person
    type: person_ref
    resolver: identity
    default: everyone
  - name: direction
    type: enum[i_owe, owed_to_me, both]
    default: both
model: none             # read path is a query; extraction happens on ingest
tier_hint: B
---

# commitments — the open-loop ledger

Answer "what did I promise, and what is owed to me", with receipts.

## Steps
1. Resolve `person` through L4 if given. An unresolved name returns a question,
   never a silent empty list.
2. Query ledger entries with `status = open`, filtered by direction.
3. Group by counterparty, sort oldest-first inside each group — age is the signal.
4. Each line: what, to/from whom, when it was said, where it was said (link), age.
5. Close with a count of items older than 14 days, if any. No advice unless asked.

## Rules
- Extraction is a separate L6 job at ingest time; this intent never re-reads raw
  messages, it reads the ledger. Read paths stay cheap and deterministic.
- Never quote message bodies into logs (§6.3). The card may quote; the log may not.
- An entry is only "closed" by evidence or by the user saying so — never by age.
- This is the same data F3's **Owed** block reads. One source, two views.

## Edge cases
| Situation | Do |
|---|---|
| Ledger is empty | Say it's empty and how far back the ledger reaches. An empty ledger and no ledger look identical to the user otherwise. |
| Name matches two people | Ask, listing both with a distinguishing detail. |
| Asked about a person the user can't see (L2) | Refuse, name the reason, do not confirm the person exists. |
| Item looks done but isn't closed | Show it flagged "possibly done" with the evidence, and offer to close it. Never auto-close. |
| Asked in a channel | Never post; DM only — the ledger is user-owned. |
| >40 open items | Show top 10 by age plus counts per counterparty, and offer the full list as a file. |
