---
name: release
description: Ship Mentor Agent to production — migrations, gates, prompt versioning, tag, and a rollback that actually works.
---

# Workflow: release

**Use when:** deploying to an environment real people receive notifications from.

Mentor writes to people's Slack DMs and to their work systems. A bad release doesn't just
break a page — it sends wrong messages to real colleagues. Treat every release as
user-visible.

## Steps

1. **PLAN + approval** (§0), including the diff summary and the rollback plan.
2. **Freeze the diff.** List every change since the last tag, grouped by layer. Flag any
   change to: prompts, salience scoring, notification budget, write-back scopes, schema.
   Those four are the risky classes.
3. **Gate.** `ruff check` · `black --check` · `pytest` · `pytest tests/identity`
   (report precision). No skipped tests, no `xfail` added in this diff.
4. **Prompt versions.** Any changed agent instruction gets a bumped version ID. Confirm
   the ID is recorded on generated cards — otherwise past cards become unreplayable.
5. **Migrations.** Run `alembic upgrade head` on a copy of prod data. Confirm a
   **downgrade path** exists, or state plainly that the migration is one-way and get that
   acknowledged before proceeding.
6. **Dry-run the rituals.** Run each ritual in **propose-only mode** against the fixture
   week. Diff the cards against the previous release. Unexplained volume changes block the
   release — a release that makes Mentor louder without a decision is a regression.
7. **Write-back safety.** Confirm no new write path is enabled by default and every
   enabled one has a consent scope. Verify the kill switch turns all write-back off
   without a deploy.
8. **Secrets & config.** No new required env var missing from `.env.example`. No secret in
   the diff (scan). Encryption key handling unchanged unless explicitly in scope.
9. **Tag & deploy.** Annotated tag, notes = the grouped diff from step 2.
10. **Watch.** First scheduled ritual after deploy: check delivery count, error rate, and
    that at least one card looks right. Abnormal volume → roll back first, diagnose after.
11. **Report.** Version / what shipped / risky classes touched / dry-run diff / rollback
    command.

## Rollback

State the exact command before deploying, not after. If the release included a one-way
migration, rollback means forward-fix — know that *before* you tag.

## Failure modes to avoid

- Shipping a prompt change without a version bump (breaks replay, breaks incident triage).
- Skipping the dry-run diff — this is the only step that catches "now it notifies 4× more".
- Enabling a write-back scope as a side effect of a refactor.
- Deploying on Friday afternoon, straight into the Friday review ritual.
