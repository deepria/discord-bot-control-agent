# Read-only observability API

All endpoints require the Agent Bearer token. They read Bot telemetry or SQLite
with bounded reads and never write to the Bot database.

- `GET /traces?from=<ISO8601>&to=<ISO8601>&limit=50&cursor=<opaque>` returns
  newest-first, content-free turn events. The maximum requested period is 31
  days.
- `GET /traces/{turn_id}` returns the content-free events for one turn.
- `GET /analytics/usage?from=<ISO8601>&to=<ISO8601>&group_by=provider` returns
  aggregated answer usage.
- `GET /memory?scope=channel|owner_private&limit=50&cursor=<id>` returns
  metadata only. It never returns stored memory content.
- `GET /memory/{item_id}` is an administrator-only metadata read. It requires
  the Console to send `X-Rio-Actor-Id`, `X-Rio-Actor-Timestamp`, and
  `X-Rio-Actor-Signature`. The signature is the lowercase HMAC-SHA256 hex value
  of `<actor_id>.<timestamp>`, signed with `RIO_CONSOLE_IDENTITY_SECRET`; the
  timestamp is valid for five minutes. Every successful read is recorded in
  `RIO_MEMORY_AUDIT_PATH` without memory content.
- `GET /data-sources` reports each source as `HEALTHY`, `STALE`, or
  `UNAVAILABLE`, including the source modification time and safe error code.

`RIO_TELEMETRY_MAX_AGE_SECONDS` controls when a readable source becomes stale
(default 900 seconds). `RIO_JSONL_READ_MAX_BYTES` bounds each JSONL tail read
(default 1 MiB).
