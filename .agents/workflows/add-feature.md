---
name: add-feature
description: Implement or change behaviour inside an existing layer of Mentor Agent, test-first and one layer at a time.
---

# Workflow: add-feature

**Use when:** implementing or modifying behaviour in an existing layer.
**Do not use when:** adding a data source (`add-connector`) or promoting a phase (`phase-gate`).

## Steps

1. **Locate the layer.** Name exactly which of the 7 layers owns this change. If the
   answer is "two or three", the feature is decomposed wrong — split it into separate
   changes, one per layer, and say so in the plan.
2. **Read before writing.** The layer's module, its tests, and the relevant section of
   `docs/` (spec v3). Note any assumption the code makes that the spec doesn't.
3. **PLAN + approval** (§0 of `AGENTS.md`). Include the layer, the files, and the exact
   verification commands. Wait.
4. **Write the failing test first.** Mandatory for `identity/`, `salience/`, `memory/`
   and anything touching user data; strongly preferred elsewhere. The test must fail for
   the right reason — show the failure output.
5. **Implement, narrowly.** Smallest change that passes. Pass `clock`, `db`, `config` in
   as arguments; don't reach for globals. No vendor SDK imports outside
   `connectors/`/`deliver/`.
6. **Check the invariants:**
   - no LLM call added in layers 1–5
   - layer only calls the layer directly below it
   - `datetime.now()` appears nowhere outside `core/clock.py`
   - any new agent instruction is a versioned file in `app/agents/prompts/`, and the
     version ID is recorded on the output
   - any new `@tool` is a thin wrapper: validate → call one layer function → typed return
7. **Widen the tests.** One happy path, one boundary, one failure. If the change affects
   what the user sees, add a snapshot of the rendered card.
8. **Run the gate.** `ruff` + `black --check` + `pytest`. If `identity/` was touched,
   also `pytest tests/identity` and report the precision number.
9. **Update docs in the same PR.** Docstrings, `AGENTS.md` if a rule changed, an ADR in
   `docs/adr/` if a decision changed.
10. **Report.** What changed / which layer / how verified / what is *not* covered.

## Failure modes to avoid

- Fixing a symptom in layer 6 that is actually a normalization bug in layer 3.
- Adding an LLM call to "handle the messy case" in layers 1–5.
- Bundling an unrelated cleanup into an approved plan.
- Making a test pass by loosening the assertion.
