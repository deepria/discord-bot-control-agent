# Discord Presence control

Deploy the Bot Presence service first, then this Agent and the Console. No new port,
database, broker, Discord client, or dependency is needed. The bot owns its SQLite
schema and runs the Discord change on its existing event loop. The agent invokes the
Bot Python service through the existing subprocess boundary, using JSON on stdin.

## API

Both routes require the existing agent Bearer token. Writes additionally recheck
`actor_id` against the Bot's `BOT_ADMIN_IDS`.

- `GET /bot/presence[?request_id=<UUID>]`
- `PUT /bot/presence`

Write body:

```json
{
  "mode": "manual",
  "status": "dnd",
  "activity_type": "playing",
  "activity_text": "코드 수정 중",
  "request_id": "b769af20-f504-48b0-9a62-e7b9f4944a2e",
  "actor_id": "123456789012345678"
}
```

Statuses: `online`, `idle`, `dnd`, `invisible`. Activities: `playing`, `watching`,
`listening`. Text is trimmed, 1–128 Unicode code points, without control characters
or surrogate code points. Streaming is unsupported in this release. Auto uses the
server-owned `online / playing / 대기 중` policy; it preserves the last Manual values.

The response contains `configured`, `manual`, `last_sent`, `last_sent_at`,
`connected`, `apply_state`, `operation`, `capabilities`, and the latest 20 content-free
audit entries. `configured` changes only when the bot completes the send and database
commit. `last_sent` means the local Gateway send completed, not acknowledgement that
all Discord clients rendered it. Presence does not change service health.

A new request returns **202** with `operation.state=queued`; query the same request
ID until it becomes `success`, `failure`, or `unknown`. A repeated completed request
returns **200** with its terminal outcome, which must be checked rather than treating
every 200 as successful application. Request ID/payload/actor conflicts or another
pending request return **409**. Invalid values return **422**, missing request IDs
**404**, and unavailable/uninitialized/disconnected Bot **503**. Polling reads the DB
without writing or refreshing the heartbeat.

## Recovery and storage

The Bot adds `bot_presence`, `bot_presence_requests`, and `bot_presence_audit` to its
existing SQLite DB. Agent processes never initialize these tables. Request payloads
are cleared on completion; activity text is absent from audit and event logs.
Idempotency metadata and audit history persist. Request ID reuse must use the same
normalized payload and actor. An accepted queue entry expires after 30 seconds.

Manual values and mode survive restart. Ready/resume restores the committed setting
before consuming new requests. A Bot restart terminates unfinished commands as
`unknown`; they are not replayed. Stale heartbeats (over five seconds) and disconnects
block new writes. Sends, recovery, and reconnects share a five-second minimum interval,
including the persisted last attempt across restart. No-op commands avoid redundant
Gateway writes. Each send has a three-second bound.

Discord IO and SQLite commit cannot be atomic. Any uncertain send/commit leaves the
committed setting intact, records an `unknown` outcome, and schedules restoration of
the committed setting. The UI must not label that outcome a successful change.

## Verification

Agent unit tests run independently. The cross-repository subprocess integration test
also runs when the Bot package is installed; otherwise only that integration test is
skipped. Use the Bot's dev dependencies for combined local verification. Actual Discord
display and production ready/resume checks remain deployment verification steps.
