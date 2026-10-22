# Read-only observability API

All endpoints require the Agent Bearer token. They read Bot telemetry or SQLite
with bounded reads and never write to the Bot database.

- `GET /traces?from=<ISO8601>&to=<ISO8601>&limit=50&cursor=<opaque>` returns
  newest-first, content-free turn events. The maximum requested period is 31
  days.
- `GET /traces/{turn_id}` returns the content-free events for one turn.
- `GET /analytics/usage?from=<ISO8601>&to=<ISO8601>&group_by=provider` returns
  aggregated answer usage.
- `GET /memory?scope=channel|owner_private&limit=50&cursor=<id>&owner_id=<user-id>` returns
  metadata only. It never returns stored memory content.
- `GET /memory/evaluation` runs the Bot's read-only `rio-memory-eval` module using
  the configured `RIO_BOT_PYTHON`, `RIO_BOT_REPO`, and `RIO_BOT_DB_PATH`. It returns
  allowlisted aggregate lifecycle/provenance counts, observation dates, fixed
  screening thresholds (50 active items / 7 days), source freshness, and check time.
  It accepts no caller-supplied database path or threshold. The tool has a five-second
  timeout; unavailable/older Bot installations return a safe 503. No source IDs,
  owners, memory content, or subprocess diagnostics are returned. Metadata readiness
  does not enable retrieval or authorize content review.
- `GET /memory/{item_id}` is an administrator-only metadata read. It requires
  the Console to send `X-Rio-Actor-Id`, `X-Rio-Actor-Timestamp`, and
  `X-Rio-Actor-Signature`. The signature is the lowercase HMAC-SHA256 hex value
  of `<actor_id>.<timestamp>`, signed with `RIO_CONSOLE_IDENTITY_SECRET`; the
  timestamp is valid for five minutes. Every successful read is recorded in
  `RIO_MEMORY_AUDIT_PATH` without memory content.
- `GET /memory/{item_id}?include_content=true` explicitly includes the stored
  memory candidate text, lifecycle, owner/scope, source message/turn IDs, and
  guild-message links under the same signed administrator authorization. It reads
  only the selected item; it never loads original conversations or calls Discord.
  A missing turn ID is returned as null, not guessed. `usage_state=shadow_only`
  describes the current writer's exclusion from the answer path. Content reads
  require a successful `memory.content.read` audit before returning, and use
  `Cache-Control: no-store`. Public lists and aggregate reports never include text.
- `GET /data-sources` reports each source as `HEALTHY`, `STALE`, or
  `UNAVAILABLE`, including the source modification time and safe error code.

`RIO_TELEMETRY_MAX_AGE_SECONDS` controls when a readable source becomes stale
(default 900 seconds). `RIO_JSONL_READ_MAX_BYTES` bounds each JSONL tail read
(default 1 MiB).

The optional owner_id is an ASCII decimal user ID (1–30 digits), compared exactly using a bound SQL parameter. It composes with disclosure and cursor pagination. It is a metadata filter, not an authorization claim; content remains signed-admin-only.

## Retained telemetry (schema 1)

`RIO_TELEMETRY_DB_PATH` defaults to `/opt/rio-discord-bot/data/telemetry.sqlite3`, the Bot's
new dedicated store. Agent opens it with mode=ro and query_only, checks its application/schema
identity, and never expires rows itself. Deploy the Bot writer before this reader.

- `/traces`, `/traces/{turn_id}`: query unexpired 30-day trace, including `turn.usage` call links.
  Stable `(at, turn_id, event)` cursors and existing response fields are preserved.
- `/analytics/usage`: up to 90 elapsed days. Keeps existing answer-call count/error/success,
  known token total, and exact observed p50/p95 semantics; adds `unknown_token_calls`.
  Discord turn aggregates and provider-call facts are not summed together.
- `coverage`: mode `retained`/`legacy_tail`, retention_days, collection_started_at,
  oldest_at/newest_at, truncated, history_limited, cleanup_pending. Bounds describe all
  currently retained records for that stream, not continuous coverage or a missing-write audit.
- An absent store falls back to explicit partial legacy-tail results. An invalid/unreadable
  existing store returns UNAVAILABLE, never a silent legacy success. Old traces remain
  excluded at read time even if the Bot expiry timer is not installed.
- Usage reads at most 100,000 samples and enforces a SQLite progress budget. Hitting sample
  or group caps reports truncated=true; a query-work failure reports UNAVAILABLE. Trace
  responses are row/byte bounded and paginated. Timestamps require a timezone.
- Collection starts with the new writer; no old JSONL imports and no claims of a preexisting
  30/90-day history. Source HEALTHY means the store was readable, not recent user activity.

Install the Bot's hourly expiry timer after reviewing its preview. Existing JSONL, host journals,
backups and memory are unaffected; no global maximum retention for those copies is asserted.
