---
name: incident-bad-notification
description: Diagnose a notification Mentor should not have sent (or should have sent) and fix it at the correct layer — suppression, never a heuristic retune.
---

# Workflow: incident-bad-notification

**Use when:** Mentor sent something wrong, useless, badly timed, or wrong about a person —
or stayed silent when it shouldn't have.

This is the product's most important repair path. A co-pilot that annoys people once gets
muted forever, and the instinct to "just tune the trigger down" is how the whole system
quietly stops working.

## Steps

1. **Reproduce from the record.** Pull the card's `trigger_id`, the context object, the
   prompt version, and the weights as they were at send time. The system is replayable —
   use that instead of guessing. If you *can't* replay it, that's the bug: fix
   observability first.
2. **Classify the failure.** Exactly one of:
   - **wrong facts** → layer 3 (normalization) or layer 4 (identity)
   - **wrong person** → layer 4, identity ladder
   - **right facts, shouldn't have spoken** → layer 5, salience / budget
   - **right call, bad wording** → layer 6, prompt
   - **right content, wrong time or channel** → layer 1 or 7
   - **user simply doesn't want this** → not a bug: a **suppression**
3. **PLAN + approval** (§0), naming the class and the layer.
4. **Fix at that layer only.**
   - **Never retune a trigger heuristic to silence one case.** Add a `suppressions` row
     with the narrowest scope that covers it (`instance` → `series` → `temporal` →
     `global`) and a TTL.
   - Remember precedence: explicit suppression beats urgency; narrower beats broader;
     suppression is **silence, not deletion** — verify the item still appears in pull
     surfaces (`/mentor prep`).
   - Identity errors: fix the tier rule, then add the pair to `tests/identity/golden.yaml`.
   - Never patch by special-casing a person's name in code.
5. **Add the regression test.** Replay the same context object; assert the new outcome.
   This test is the deliverable — the fix without it will regress.
6. **Check the blast radius.** Would this fix silence something the user *does* want?
   Run the ritual over the fixture week and diff the cards produced before/after. Report
   the diff.
7. **Feed layer 7.** If the user has now overridden the same thing repeatedly, queue a
   *proposal* ("mute standup briefings permanently?") — propose, never auto-commit.
8. **Report + one line to the user.** In the product voice: say it plainly, correct it,
   don't apologise three times (`SOUL.md`).

## Failure modes to avoid

- Lowering a threshold globally to fix one meeting. Silences things nobody complained about.
- Deleting the item instead of suppressing it — the user can no longer find it on purpose.
- Fixing a wording problem by changing the trigger, or a trigger problem by changing the prompt.
- Shipping the fix without the replay test.
