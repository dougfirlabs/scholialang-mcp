# Durable capability state

`scholialang_mcp.durable_store.DurableCapabilityStore` is the authoritative
local storage seam for capability adapters. It uses a real SQLite file and
requires `enabled=True`; construction with the default policy refuses before
opening a file. No environment variable, wire capability, or server discovery
advertisement is enabled by this change. Events/Tasks/Heartbeat wire adapters
remain the separate SCH073-07 slice. The supplied cumulative base had no
capability-store interface or in-memory implementation to replace.

```python
from pathlib import Path
from scholialang_mcp.durable_store import DurableCapabilityStore, Scope

# The host provisions a private parent directory and selects both the path and
# authenticated scope. These must not come from untrusted tool arguments.
store = DurableCapabilityStore(
    Path("/private/disposable-project/capabilities.sqlite3"),
    Scope(owner="authenticated-owner", project="authorized-project"),
    enabled=True,
)
record = store.put(
    "tasks", "task-123", {"status": "working"},
    idempotency_key="request-123", expected_revision=0,
)
snapshot = store.refetch()
store.dispatch(lambda intent: print(intent))
```

Use one database per trusted owner/project. The persisted identity is checked
on every operation and reopening with a foreign owner or project is denied.
This is defense against scope mixups, not authentication or protection from a
user with direct filesystem access. The host owns path authorization, private
directory permissions, overall project/database count, and facet policy. No
operator DAG database is opened or migrated. There is no in-memory fallback.

## Transaction and delivery contract

Each `put` reserves the SQLite writer with `BEGIN IMMEDIATE`, validates the
scope, prunes expired rows, checks its scoped idempotency receipt and optional
record revision, then writes state, a monotonically increasing scope revision,
notification intent, and retry receipt in **one transaction**. It returns the
handle only after commit. `expected_revision=0` is create-only; a positive
revision prevents lost updates. A reused idempotency key with a different
request fails. A response-loss retry of the identical request returns its
original receipt without a second revision or intent, even if state was later
updated. The receipt is a historical response; `refetch()` returns current state.

The backend uses rollback-journal `DELETE` mode and `synchronous=EXTRA`, which
also syncs the directory after journal deletion. SQLite provides atomic crash
recovery; its durability assumes a local filesystem that implements locking
and sync correctly. See [SQLite atomic commit](https://www.sqlite.org/atomiccommit.html)
and [synchronous settings](https://www.sqlite.org/pragma.html#pragma_synchronous).
These tests exercise process death, native database-full errors and commit-lock
failure; they do not simulate a machine power cut or broken storage firmware.

`dispatch` reads committed intents on a separate connection before invoking
the host callback. Callback success is recorded afterward. A failed callback
or crash before that marker leaves the intent retryable; a crash after sending
can therefore duplicate delivery. Use one dispatcher per scope for ordered
delivery attempts. Concurrent dispatchers do not corrupt state but may duplicate
or reorder notifications. Notification intents carry bounded kind/key/digest
metadata, not task results or payloads. The host maps these to the negotiated
wire contract and applies subscription filters and acknowledgement ordering.

Reconnect first resubscribes at the transport, then calls `refetch` for a
consistent committed snapshot plus its scope revision. The internal `events`
API accepts that revision; an evicted/expired prefix returns `resync_required`.
Delivered events remain available until retention expiry. Internal cursors and
delivery markers are **not** invented standard MCP replay or event-ack methods.
There is no exactly-once delivery guarantee. Slow/disconnected consumers must
refetch if retention removes pending notifications.

## Bounded retention and time

Defaults per database (persisted and checked when reopening):

| Item | Bound |
| --- | --- |
| State | 256 current records; maximum lifetime 24 hours |
| Outbox | 512 digest-only intents; maximum lifetime 1 hour |
| Retry receipts | 512 original responses; maximum lifetime 1 hour |
| JSON payload | 16 KiB encoded per record |
| Kind, key, owner, project, retry key | 256 UTF-8 bytes each |
| SQLite main file | 64 MiB hard page limit on every connection |

Shorter per-write TTLs also shorten its intent and receipt lifetime. Expiry is
inclusive (`expires_ms <= now`). Receipts expire independently of newer state;
after the retry window, a key may name a new request. Count overflow rejects new
records/receipts without evicting live state or breaking a promised retry
window. Outbox count overflow evicts a prefix and advances its persistent
resync floor. Expiry also removes a prefix (possibly including older live
intents when a later intent has a shorter TTL), conservatively requiring
refetch. Revisions never reset after all records expire.

Reads filter expired rows without modifying their committed snapshot. Writes
and explicit `prune()` physically remove expired rows. An idle host should call
`prune()` periodically, and on reconnect when practical. Stopped hosts retain
bounded on-disk rows until the next write/prune; this is not a secure-erasure
policy. SQLite reuses freed pages; the file can retain its bounded high-water
size. DELETE journaling avoids a growing WAL and removes the journal on commit;
budget additional space roughly equal to the main file plus journal overhead
for an active transaction or hot journal after a crash. The process-crash tests
verify hot-journal recovery using fresh interpreters.

Time is Unix wall-clock milliseconds, injected only for deterministic tests.
Successful writes/prune persist a high-water clock; later operations clamp to
it. A backward jump cannot resurrect physically pruned rows. Read-only expiry
is evaluated against max(wall clock, last committed watermark); a backward jump
after an unpersisted read may temporarily change visibility until maintenance.
Hosts needing a strict cross-restart expiry observation must call `prune()`.
Large forward jumps expire rows early; clock synchronization is host-owned.

## Errors, schema, and rollback

`StoreError.code` identifies disabled policy, invalid input, foreign scope,
revision/idempotency conflicts, retention mismatch, resync, and quota failures.
Native `sqlite3.Error` (including full/locked/corrupt/I/O errors) propagates;
adapters must classify it as storage failure, never as success. An ambiguous
commit failure is resolved by retrying with the original idempotency key and
refetching. No callback is invoked in a transaction or automatically on `put`.

Only schema version 1 is accepted. An empty new database is initialized
atomically; unknown schemas and changed retention policy fail closed. Existing
files are not destructively migrated. Retention changes require an explicit
future migration. Disable the host's opt-in to roll back and retain the database
and evidence for inspection; restore the prior application receipt/ref. Do not
downgrade this file into an operator DAG database.

## Reproducible validation

Run `PYTHONPATH=src /usr/bin/python3 -m pytest
tests/integration/test_durable_store.py --timeout=10 -v -s` with the repository's
declared development dependencies installed in a disposable environment.
All data is synthetic, but the backend, filesystem, independent subprocesses,
SQLite transactions, and recovery are real. An in-memory fixture is not used
as durability evidence. Tests emit crash-stage and retention-size measurements.
