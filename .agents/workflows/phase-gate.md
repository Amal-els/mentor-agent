---
name: phase-gate
description: Verify a Mentor Agent phase (P1-P4) is genuinely complete before starting the next one, so later features never get hacked into earlier layers.
---

# Workflow: phase-gate

**Use when:** claiming a phase is done, or before starting work belonging to the next phase.

Phases exist because all five features stay in scope — breadth is kept, delivery is
staged. The gate's job is to stop P3 behaviour from being smuggled into P1 code.

## Phases

The morning pulse (F3, all 7 layers) shipped ahead of `/mentor prep` (F1) —
see AGENT.md §9 and `docs/plans/morning-pulse.md` for why. The table below
is the current phase, not the original P1–P4 split (kept in AGENT.md §9 for
its sequencing of the *remaining* features).

| Phase | Contains | Status |
|---|---|---|
| **Pulse** | Layers 1–7, morning pulse (F3) end to end, identity graph (already existed), Postgres | **Shipped** |
| **Prep** | `/mentor prep` (F1), dossier card, Calendar + Slack | Not started |
| **Agenda/Ledger** | Rolling 1-on-1 agenda (F2), Accomplishment Ledger (F5), consented write-back | Not started |
| **Review** | Friday review (F4), feedback signals | Not started |
| **Learning** | Weights + declarative memory + suppression surfaces, HRIS, encryption hardening | Not started |

## Gate checklist (all must pass, with evidence)

1. **Feature demo.** Every feature listed for the phase runs end-to-end from a real
   trigger, not a test harness. Attach the rendered cards.
2. **Layer discipline.** No module imports a layer more than one below it. No LLM call in
   layers 1–5. Grep and show the result.
3. **No future-phase shortcuts.** Search for stubs, `TODO(P3)`, hard-coded values standing
   in for a later layer. Any that remain are listed explicitly in the report as known debt,
   or removed.
4. **Tests.** Full gate green. Coverage exists for each new layer's happy path, boundary
   and failure. From P2 on: `pytest tests/identity` precision ≥ 0.99, number reported.
5. **Privacy.** Every data field in use appears in `docs/data_inventory.md`. No message
   bodies or ledger content in logs — grep the log calls. From P3: write-back requires a
   consent scope and defaults to propose-only. From P4: per-user encryption verified with
   a test that a second user's key cannot read the first user's ledger.
6. **Observability.** Every delivered card is replayable: context object + weights +
   prompt version recorded. Required before P3, because `incident-bad-notification`
   depends on it.
7. **Docs & diagrams.** Spec and Excalidraw diagrams match the code that exists
   (run `regen-docs`). `AGENTS.md` reflects any changed rule.
8. **Migrations.** `alembic upgrade head` from empty, and from the previous phase's schema.

## Output

A short phase report: what shipped / evidence per checklist item / known debt carried
forward / what the next phase now depends on. Only after that, plan P(n+1).

## Failure modes to avoid

- Declaring a phase done because the code exists but was never run from a real trigger.
- Carrying silent debt instead of listing it.
- Building P4's weights early "since it's easy" — it makes P2's salience untestable.
