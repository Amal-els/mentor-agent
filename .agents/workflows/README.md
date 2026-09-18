# Dev workflows

Named procedures for the coding agent. A workflow exists only where **order matters and
getting it wrong is expensive**. Everything else is just work — do it.

| File | Use when |
|---|---|
| `add-feature.md` | Any behaviour change inside an existing layer |
| `add-connector.md` | Adding or extending an MCP data source |
| `phase-gate.md` | Promoting P1→P2→P3→P4 |
| `incident-bad-notification.md` | Mentor said something it shouldn't have |
| `release.md` | Shipping to prod |
| `regen-docs.md` | Diagrams / spec drifted from code |

## Rules that apply to every workflow

1. **§0 of `AGENTS.md` wins.** Post the PLAN, wait for approval, then start.
2. Steps run in order. If you must reorder or skip, stop and re-propose — don't
   improvise silently.
3. Every workflow ends with a **report**: what changed / how it was verified / what was
   *not* covered.
4. If a workflow's steps turn out to be wrong or missing something, fix the workflow file
   in the same PR as the code. A stale workflow is worse than none.

Definition of done shared by all: `uv run ruff check . && uv run black --check . && uv run pytest`
passes, and nothing new is undocumented.
