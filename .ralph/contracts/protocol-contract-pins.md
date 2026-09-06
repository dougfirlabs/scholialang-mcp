# Sch073 PRD04–07 protocol contract pins and reuse audit

Read-only source audit performed 2026-09-05 by dfpmcodex's independent protocol reviewer. This report is evidence and implementation guidance, not acceptance of an implemented adapter. No product files, refs, services, or runs were changed by the reviewer.

## Authoritative pins

| Contract | Immutable source | Selected path / revision |
| --- | --- | --- |
| MCP core | `modelcontextprotocol/modelcontextprotocol@e76e9c572c6f2bfcb730357101acc90f2f802e02` | `schema/2026-07-28/schema.ts`, `schema.json`; `docs/specification/2026-07-28/` |
| MCP Tasks extension | `modelcontextprotocol/experimental-ext-tasks@9263312d11a682ac83f83fe84794d4627efd22f5` | `schema/2026-07-28/schema.ts`, `schema.json`; `specification/2026-07-28/tasks.md` |
| Python SDK inspected | `modelcontextprotocol/python-sdk@0921d94a74db900dccd2d534842aa7b6160542d2` | release `v2.1.1`; modern subscriptions runtime exists; modern Tasks runtime explicitly deferred |
| Existing portable Heartbeat | OT `cb87379bbe4e26a8e59bab9cc18dc51ef4079bff` | `packages/mcp-heartbeat/`; distribution `0.1.0`, lease contract `0.1`; optional current SDK pins `mcp==2.0.0`, `mcp-types==2.0.0` |

The dated modern Tasks snapshot is **stable**, despite the repository's historical `experimental-ext-tasks` name. Do not select the moving draft merely because a search result lands there. Modern core subscriptions are also stable, not a proposed Events extension. Preserve exact fetched bytes/hashes in the eventual implementation evidence; this inventory records immutable Git source pins, not built-artifact hashes.

Primary source links:

- [Core specification](https://modelcontextprotocol.io/specification/2026-07-28)
- [Pinned core subscription contract](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/e76e9c572c6f2bfcb730357101acc90f2f802e02/docs/specification/2026-07-28/basic/patterns/subscriptions.mdx)
- [Pinned core schema](https://github.com/modelcontextprotocol/modelcontextprotocol/blob/e76e9c572c6f2bfcb730357101acc90f2f802e02/schema/2026-07-28/schema.ts)
- [Tasks stable-versus-draft release inventory](https://github.com/modelcontextprotocol/experimental-ext-tasks/blob/9263312d11a682ac83f83fe84794d4627efd22f5/README.md)
- [Modern Tasks normative document](https://tasks.extensions.modelcontextprotocol.io/specification/2026-07-28/tasks)
- [Pinned modern Tasks schema](https://github.com/modelcontextprotocol/experimental-ext-tasks/blob/9263312d11a682ac83f83fe84794d4627efd22f5/schema/2026-07-28/schema.ts)
- [Legacy Tasks specification](https://modelcontextprotocol.io/specification/2025-11-25/basic/utilities/tasks)
- [Python SDK Tasks runtime deferred notice at inspected release](https://github.com/modelcontextprotocol/python-sdk/blob/0921d94a74db900dccd2d534842aa7b6160542d2/examples/stories/tasks/README.md)

## Minimal era/feature matrix

| Facet | Modern MCP `2026-07-28` | Legacy |
| --- | --- | --- |
| Discovery | `server/discover`; version and client capabilities on each request | `initialize`; lifecycle-negotiated capabilities |
| Events | Core `subscriptions/listen`, requested filters, ack-first tagged notification stream | `resources/subscribe` / `resources/unsubscribe`, resource/list change notifications |
| Tasks | Separate `capabilities.extensions["io.modelcontextprotocol/tasks"]`; server-directed augmentation; `tasks/get`, `tasks/update`, `tasks/cancel` | `2025-11-25` experimental-in-core Tasks uses different negotiation and `tasks/result` / `tasks/list`; earlier revisions have no standard Tasks |
| Heartbeat | Vendor `com.dougfirlabs/heartbeat`, lease contract `0.1`, current portable adapter | Vendor `experimental.presenceLease`, legacy portable adapter supports `2025-06-18` and `2025-03-26` only |

Recommended minimum claim: implement modern subscriptions and modern Tasks against these pinned schemas; preserve explicitly synchronous behavior for legacy clients unless a separately tested legacy Tasks adapter is implemented. Do not imply support for every legacy feature merely because the transport accepts that protocol version.

## PRD04 negotiation / transport constraints

- Local Scholialang-MCP stage `f08062fb9f3ea56645ab1ba1c75a177e21c29e9f` uses handwritten stdio transports and has no MCP SDK dependency in `pyproject.toml`. Source atlas and plugin servers advertise tools only; their protocol-version conformance does not prove Events, Tasks, or Heartbeat.
- The wheel currently accepts modern 2026-07-28 and legacy 2025-11-25 / 2025-06-18 / 2025-03-26 / 2024-11-05. The generated plugin matrix excludes 2024-11-05. Preserve this intentional matrix rather than silently widening it.
- Use distinct on-wire era adapters. Modern requests cannot inherit task support from an earlier request or an SDK/package version.
- New facets default off. An internal `scholialang.mcp-capabilities.v1` advertisement is an OT integration seam, not a substitute for MCP wire negotiation or conformance evidence.
- Official Python SDK v2.1.1 supports the modern protocol and subscriptions, but its Tasks example explicitly says runtime deferred. Legacy task types in `mcp_types` are not a modern Tasks service.

## PRD05 Events conformance traps

- `subscriptions/listen` takes a notifications filter: `toolsListChanged`, `promptsListChanged`, `resourcesListChanged`, `resourceSubscriptions`.
- First notification per subscription is `notifications/subscriptions/acknowledged`, reflecting only supported requested filters. Do not emit unrequested types.
- Every notification carries `_meta["io.modelcontextprotocol/subscriptionId"]`, equal to the original JSON-RPC request ID. Multiple subscriptions share stdio and need independent correlation.
- StdIO cancellation uses `notifications/cancelled` for the originating request ID; HTTP cancellation closes that subscription's response stream. Server graceful closure responds to the original request with a complete result.
- Reconnect requires resubscription and authoritative refetch. Standard subscriptions do **not** define a durable event-ack RPC, universal cursor/replay method, or exactly-once delivery.
- PRD-required outbox, scoped revisions, deduplication, bounded queues and resync detection can remain internal/application-level state. Do not overload standard notifications with invented mandatory fields. Explicit namespaced metadata must remain safely ignorable.

## PRD06 modern Tasks conformance traps

- Capability is `extensions["io.modelcontextprotocol/tasks"]: {}`. Client declares it per request; server declares it in discovery. Server chooses whether an eligible tools/call becomes a task.
- Creation returns flat `resultType: "task"` plus task fields, not `{task: ...}`. Use `ttlMs` / `pollIntervalMs`, not legacy `ttl` / `pollInterval`.
- `tasks/get` returns `resultType: "complete"` plus detailed task state and appropriate inline `inputRequests`, `result`, or `error`.
- `tasks/update` submits keyed `inputResponses` for active input requests. There is no modern `tasks/provide_input` or `tasks/result` method. An MRTR retry of the original request is distinct from input for an already-created task.
- A tool result with `isError: true` maps to `completed` with that result. `failed` is for JSON-RPC execution errors. This differs from legacy Tasks.
- Modern cancellation acknowledges intent with a complete empty result. Execution may remain working and race to completion; an immediate cancelled terminal status is not guaranteed. Preserve real execution/effect evidence separately.
- Optional `notifications/tasks` can be requested with `taskIds` in `subscriptions/listen`; do not reuse legacy `notifications/tasks/status`. Tasks remains independently pollable.
- HTTP task operations route `Mcp-Name` from taskId. Principal/project authorization, scoped idempotency and durable-before-handle creation remain required application responsibilities.
- The normative prose contains a stale reference to an embedded `task` near its creation example, but the actual dated schema and example agree on the flat shape. Pin schema fixtures to avoid perpetuating that typo.

## PRD07 Heartbeat: reuse an existing vendor contract

The normative definition exists locally; no new standards-body namespace or lease schema is needed.

- Portable core: `packages/mcp-heartbeat/src/mcp_heartbeat/`
- Normative lease: `packages/mcp-heartbeat/docs/heartbeat-0.1.md`
- Schema: `packages/mcp-heartbeat/schema/mcp-heartbeat-0.1.schema.json`
- Modern adapter: `packages/mcp-heartbeat/src/mcp_heartbeat_current/`
- Legacy adapter: `packages/mcp-heartbeat/src/mcp_heartbeat_legacy/`
- Modern pins and protocol constants: `src/mcp_heartbeat_current/contract.py` beneath that package.
- Host-only boundary: `src/opentalon/integrations/mcp_presence/heartbeat_boundary.py`.

The six wire fields are extension_version, node_id, boot_id, sequence, issued_at, expires_at. Keep wire names unchanged, clocks/lineage/identity distinct, and HTTP availability or ping separate from lease validity. The vendor identifier is deliberately not `io.modelcontextprotocol/heartbeat`. Current/legacy adapters and the stdlib-only core are suitable reuse candidates; Scholia-specific resources, principal policy injection and transport binding still need independent tests.

Optional SDK conformance runs in its own environment: OT's current adapter pins can conflict with the shared venv's pydantic-core. Never install that extra into OT's owning venv as a shortcut.

## Existing OT Tasks/Events reuse assessment

### Historical Tasks/MRTR prototype: reject its wire implementation

Exact branch `feat/mcp-2026-07-28-prd-03-tasks-mrtr` at `f14501ba3b1328017b6bc3209bb455ed1c59ffc7`:

- `src/opentalon/mcp_servers/tasks_prototype/broker.py`
- `src/opentalon/mcp_servers/tasks_prototype/server.py`
- `tests/unit/mcp/test_tasks_prototype.py`
- `docs/standards/2026-07-29-mcp03-tasks-mrtr-lifecycle-mapping.md`

These prototype source paths are **not present in current OT stage** `cb87379bbe4e26a8e59bab9cc18dc51ef4079bff`. A completed historical PRD status does not promote them into current code or prove stable-contract conformance.

Useful design/test ideas: journal rehydration without fabricating completion, expiring hashed approval tokens, input replay receipts, distinct task/run identity, cooperative worker cancellation.

Reject wholesale reuse because server.py advertises `tasks/provide_input` and `tasks/result`, reads client augmentation from `_meta`, returns nested `{resultType:"task",task:...}`, conflates task input with MRTR requestState, and does not enforce the modern per-request capability boundary. Its discovery uses `protocolVersions`, not stable `supportedVersions`. It is an in-process dictionary prototype, not a transport runtime. The broker's JSONL append has no fsync or cross-process transaction protocol; a one-shot plaintext input token cannot be refetched after response loss. These need redesign and adversarial tests, not a rename.

### GOAT adapter on current stage: reuse concepts, not an MCP task service

Exact current OT base `cb87379bbe4e26a8e59bab9cc18dc51ef4079bff`:

- `src/goat/mcp/tasks.py` — `GoatTaskService` exposes policy-gated Work Profile read/mutate operations and immutable receipt evidence. It is not a standard tasks/get/update/cancel wire implementation.
- `src/goat/mcp/events.py` — bounded project-scoped digest-only event projections, explicit cursor expiry and subscription limits are useful patterns. `GoatEventLog` stores events/cursors/subscribers in memory; it is not a durable outbox or standard subscriptions/listen adapter.
- `src/goat/store.py` — `FileWorkStore` provides fsync, atomic replacement and per-work flock; reusable persistence design, but its stored schema and transitions are GOAT Work Profile-specific.
- `src/goat/mcp/heartbeat.py` — read-only composition of lease/process/task/adjudication evidence; useful OT interop fixture, not portable protocol ownership.

`GoatTaskService.mutate` commits store state then appends to the in-memory event log under a local lock. Process crash can occur between those operations; the lock is not an atomic durable notification-intent transaction. Preserve this as a host-specific adapter, do not claim it satisfies PRD05 durable state+outbox acceptance.

### Extraction conclusion

There is no already-portable modern Tasks/Events package in current OT `packages/` to import as the finished implementation. Heartbeat is the existing portable package. Prefer a Scholia-owned contract/store/transport adapter plus OT fixtures; reuse bounded projections, durable-store techniques and test scenarios selectively without importing all of OT/GOAT into Scholialang-MCP.

No fresh runtime conformance suite was executed by this reviewer; claims above concern inspected source and pinned authoritative contracts only.
