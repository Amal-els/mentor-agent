---
name: quiet
command: /mentor quiet
aliases: [quiet, mute, stop, be quiet, not now, silence, pas maintenant, tais-toi, snooze]
kind: control
feature: cross-cutting (L5 suppression)
layer_entry: 5
visibility: private
slots:
  - name: scope
    type: enum[instance, series, temporal, global]
    default: temporal
  - name: until
    type: duration_or_time
    default: tomorrow 09:00 user tz
model: none             # control intent — must never touch a model
tier_hint: A            # exact/deterministic: it must work when the user is annoyed
---

# quiet — suppression, not deletion

The user is telling you to stop. Obey first, be precise second.

## Steps
1. Parse scope and duration deterministically. "quiet" alone = temporal until
   tomorrow 09:00. Never call an LLM here (§ control intents are model-free).
2. Write one row to `suppressions` (scope, target, TTL, reason = "user_explicit").
3. Confirm in one line, stating exactly what was silenced and until when, plus how
   to undo: "muted the standup series until Friday — `/mentor quiet off` to undo".
4. Emit a hard feedback signal to L7. Explicit mute may apply to weights instantly.

## Rules
- Suppression means **silence, not deletion**. Pull surfaces still show everything.
- Narrower scope wins over broader; explicit user suppression beats urgency (§5).
- Never fix a bad notification by retuning a heuristic — that's what this exists for.
- Structural floors survive: a manager 1-on-1 can be suppressed, never decayed out.
- Repeated overrides on the same target feed the L7 proposal pipeline: after the
  third, *propose* a durable rule; the user commits it. Never self-commit.

## Edge cases
| Situation | Do |
|---|---|
| "quiet" with no target, mid-conversation | Temporal scope, whole agent, until tomorrow 09:00. Confirm in one line. |
| "stop telling me about X" | Series scope on X, no TTL, plus the undo hint. |
| Ambiguous target | Choose the narrowest plausible scope, act, and say what you assumed. Never ask before obeying. |
| Already suppressed | Say it's already muted and until when. Do not stack rows. |
| "quiet forever" | Global scope, TTL null, and warn once that F1/F2 pushes stop too. |
| Suppressed thing becomes urgent | Stay silent. Urgency never overrides explicit suppression (§5) — it surfaces on pull. |
| Sent in a channel | Apply it, reply ephemeral. Do not announce a mute publicly. |
