---
name: regen-docs
description: Bring Mentor Agent's spec, diagrams and rules files back in sync with the code that actually exists.
---

# Workflow: regen-docs

**Use when:** the code moved and the docs didn't — before a phase gate, before a release,
and before any defense or review where someone reads the spec and believes it.

## Steps

1. **Inventory the drift.** For each of the 7 layers, compare the spec section to the
   module: what exists in code but not in docs, what's documented but unbuilt, what's
   described differently. Produce the list before changing anything.
2. **PLAN + approval** (§0) — including whether reality or the doc is wrong. Sometimes the
   code drifted from a deliberate design and the *code* should change; that's a different
   workflow, so stop and say so.
3. **Update in this order**, because each depends on the last:
   1. `AGENTS.md` — rules and invariants (the source of truth for behaviour)
   2. `SOUL.md` — only if the product's voice or refusals actually changed
   3. `docs/spec.md` — layer-by-layer description
   4. diagrams (`*.excalidraw` → rendered figures)
   5. `docs/data_inventory.md` — must match the fields the connectors really fetch
   6. `docs/adr/` — one ADR per decision that changed, dated, with the old decision
      referenced rather than deleted
4. **Mark unbuilt things as unbuilt.** Anything in the spec that doesn't exist yet gets an
   explicit phase tag (`P3`). A spec that reads as if everything is built is the single
   easiest way to get caught in a review.
5. **Regenerate figures** and eyeball every one. A stale diagram is more damaging than a
   missing one — people trust pictures.
6. **Cross-check the glossary.** Especially the two senses of "skill file": dev-side
   `.agents/skills/` vs product-side declarative memory (layer 4b).
7. **Report.** Drift found / what was corrected / what is still knowingly out of date.

## Failure modes to avoid

- Rewriting the spec to match a bug instead of fixing the bug.
- Deleting a superseded ADR instead of superseding it.
- Regenerating diagrams without looking at them.
- Letting the spec describe P3 features in the present tense.
