# Data inventory — M2 (L2 connectors + L3 normalize)

One row per field actually read by `app/ingest/*` from a fixture-backed
source (or, once a live connector exists, its raw API payload). Required by
the phase-gate checklist (`.agents/workflows/phase-gate.md` §5). Updated in
the same commit as any new field read.

| Field | Source | Read by | Stored as | Notes |
|---|---|---|---|---|
| `owner.id` | fixture/owner | `app/ingest/seed.py:_ensure_user` | `User.id` | primary key, also the `OwnerScope` owner |
| `owner.tz` | fixture/owner | `_ensure_user` | `User.tz` | IANA tz name |
| `owner.pulse_fire_time_local` | fixture/owner | `_ensure_user` | `User.pulse_fire_time_local` | `HH:MM`, consumed by M5 triggers |
| `owner.late_cutoff_local` | fixture/owner | `_ensure_user` | `User.late_cutoff_local` | `HH:MM`, consumed by M5 window selection |
| `people[].id` | fixture/people | `app/ingest/seed.py:_ensure_roster` | `Person.id` | roster bootstrap only, not an ingestion source |
| `people[].canonical_name` | fixture/people | `_ensure_roster` | `Person.canonical_name` | |
| `people[].primary_email` | fixture/people | `_ensure_roster` | `Person.primary_email` | matched against by `identity_seam.resolve_actor` |
| `people[].is_self` | fixture/people | `_ensure_roster` | `Person.is_self` | |
| `events[].external_id` | calendar | `app/ingest/normalize.py:normalize_event` | `Event.external_id` | upsert key half |
| `events[].source` | calendar | `normalize_event` | `Event.source` | upsert key half; always `"calendar"` |
| `events[].title` | calendar | `normalize_event` | `Event.title` | |
| `events[].starts_at` | calendar | `normalize_event` | `Event.starts_at` | tz-aware, also drives `FixtureCalendarClient` window filtering |
| `events[].ends_at` | calendar | `normalize_event` | `Event.ends_at` | tz-aware |
| `events[].status` | calendar | `normalize_event` | `Event.status` | |
| `events[].url` | calendar | `normalize_event` | `Event.url` | optional |
| `events[].series_id` | calendar | `normalize_event` | `Event.series_id` | optional; consumed by M3's series-scope suppression |
| `events[].actor_reference_key` | calendar | `normalize_event` → `identity_seam.resolve_actor` | `Event.actor_reference_key`, `Event.resolved_person_id` | resolved by exact provider ID (Identity cache) or primary email only — see `docs/plans/morning-pulse.md`'s "Not doing" |
| `work_items[].external_id` | linear (jira: stub) | `normalize_work_item` | `WorkItem.external_id` | upsert key half |
| `work_items[].source` | linear | `normalize_work_item` | `WorkItem.source` | upsert key half |
| `work_items[].title` | linear | `normalize_work_item` | `WorkItem.title` | |
| `work_items[].status` | linear | `normalize_work_item` | `WorkItem.status` | |
| `work_items[].url` | linear | `normalize_work_item` | `WorkItem.url` | optional |
| `work_items[].due_at` | linear | `normalize_work_item` | `WorkItem.due_at` | optional |
| `work_items[].updated_at` | linear | `normalize_work_item` | `WorkItem.updated_at` | optional |
| `work_items[].actor_reference_key` | linear | `normalize_work_item` → `identity_seam.resolve_actor` | `WorkItem.actor_reference_key`, `WorkItem.resolved_person_id` | same resolution rule as events |
| `messages[].external_id` | slack | `normalize_message` | `Message.external_id` | upsert key half |
| `messages[].source` | slack | `normalize_message` | `Message.source` | upsert key half |
| `messages[].channel` | slack | `normalize_message` | `Message.channel` | |
| `messages[].sent_at` | slack | `normalize_message` | `Message.sent_at` | tz-aware, also drives `FixtureSlackClient` window filtering |
| `messages[].url` | slack | `normalize_message` | `Message.url` | optional |
| `messages[].is_dm` | slack | `normalize_message` | `Message.is_dm` | |
| `messages[].body_ref` | slack | `normalize_message` | `Message.body_ref` | **pointer only** — message content is never read or stored (AGENT.md privacy rule); fixtures enforce a `fixture://` prefix |
| `messages[].actor_reference_key` | slack | `normalize_message` → `identity_seam.resolve_actor` | `Message.actor_reference_key`, `Message.resolved_person_id` | same resolution rule as events |
| `commitments[].description` | ledger (fixture-seeded in M2; F5 writes it in P3) | `seed.py:_ensure_commitments` | `Commitment.description` | |
| `commitments[].promised_to_person_id` | ledger | `_ensure_commitments` | `Commitment.promised_to_person_id` | optional |
| `commitments[].promised_at` | ledger | `_ensure_commitments` | `Commitment.promised_at` | |
| `commitments[].due_at` | ledger | `_ensure_commitments` | `Commitment.due_at` | optional |
| `commitments[].delivered_at` | ledger | `_ensure_commitments` | `Commitment.delivered_at` | optional |
| `commitments[].status` | ledger | `_ensure_commitments` | `Commitment.status` | |
| `commitments[].source_reference_key` | ledger | `_ensure_commitments` | `Commitment.source_reference_key` | idempotency key half (with `description`) |
| `unhealthy_sources[source].status` | fixture-only (health simulation) | `app/ingest/fixture_source.py:_health_for` | not persisted | drives `SourceClient.health()`; feeds M4/M6's degradation line |
| `unhealthy_sources[source].stale_as_of` | fixture-only | `_health_for` | not persisted | only present when `status: error` |

## Provenance

Every normalized row gets a `RawIngestRef` (`owner_user_id, source, external_id,
canonical_table, canonical_id, fetched_at`) — a pointer back to where the row
came from, not a copy of the raw payload. `checksum` is unused in this phase
(`None`).
