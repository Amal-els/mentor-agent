---
name: add-connector
description: Add or extend an MCP data source in Mentor Agent, including fixtures, normalizer, privacy inventory and consent scope.
---

# Workflow: add-connector

**Use when:** adding a new MCP source (Outlook, Jira, Lattice, Workday…) or fetching a
new *kind* of data from an existing one.

New data is the highest-risk change in this codebase: it touches privacy, identity, and
salience all at once. The order below is not negotiable.

## Steps

1. **Justify the data.** One sentence per field: what is fetched, which feature needs it,
   what breaks without it. Fields nothing needs are not fetched. Ever.
2. **PLAN + approval** (§0). Must include the field list from step 1.
3. **Write `docs/data_inventory.md` first.** Source, fields, purpose, retention, consent
   scope. Writing this before the code is the point — it's where over-collection gets
   caught.
4. **Record fixtures.** Synthetic, hand-written or scrubbed — never real workspace data,
   never real names or emails. Save to `tests/fixtures/{source}/`. Include at least one
   ugly payload: missing field, null, wrong timezone, deleted user, renamed handle.
5. **Implement the client** in `connectors/{source}.py` behind the `SourceClient`
   protocol. Raw payloads out, no interpretation, **no LLM call**. Handle auth, paging,
   rate limits and partial failure here — a dead source must degrade the ritual, not
   crash it.
6. **Implement the normalizer** in `normalize/{source}.py` → canonical `Event` /
   `Message` / `WorkItem` / `Goal`. Timezone-aware, deduplicated, provenance-stamped
   (`source`, `external_id`, `fetched_at`).
7. **Register identities.** Every actor in the payload gets fed to the identity graph with
   its tier (email / IdP / handle / name). Add the new source's identity examples to
   `tests/identity/golden.yaml`, then run `pytest tests/identity` — **precision must stay
   ≥ 0.99**. If it drops, fix the tier rules, don't lower the gate.
8. **Consent scope.** Declare the scope in config; default read-only. No write-back path
   in this PR — write-back is a separate, separately-approved change.
9. **Salience awareness.** State how the new signal affects scoring. If it makes Mentor
   *louder*, adjust the notification budget in the same PR or explain why it doesn't.
10. **Tests:** client against fixtures (no HTTP mocking), normalizer round-trip, ugly
    payloads, one end-to-end ritual run with the source enabled and one with it dead.
11. **Gate + report.** Full test gate, then report including the identity precision number
    and the inventory diff.

## Failure modes to avoid

- `SELECT *` thinking: fetching whole objects "in case we need it later".
- Real data in fixtures. Assume fixtures leak.
- A new source that silently doubles the notification volume.
- Logging payload bodies. Log IDs, counts, decisions — never content.
