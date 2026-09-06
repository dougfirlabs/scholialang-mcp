# Modern stdio capability adapters

The wheel provides `WireAdapters` for pinned MCP `2026-07-28` subscriptions,
Tasks, and vendor Heartbeat `0.1`. Each facet defaults to **off**. The ordinary
CLI and generated host plugins retain their existing tools-only behavior; they
do not construct these adapters or advertise their capabilities. Install the
wheel's `adapters` extra only in the embedding host's isolated environment.
No HTTP transport, deployment, credential service, or production activation is
part of this implementation.

## Trusted embedding boundary

An embedding host constructs a `HostBinding` with its configured local
principal/project `Scope`, an `AdapterPolicy`, an authorization callback and
an **independent certification verifier**. It constructs a matching
`DurableCapabilityStore` in a private host-provisioned directory, then passes
`WireAdapters(binding, store, task_tools=..., participant=...)` through
`ScholiaServerConfig(..., adapters=runtime)` to `serve_stdio(server)`.

The verifier receives `(facet, implementation_digest, protocol_pins)`. It must
check an independently issued receipt for the installed code/schema digest
and exact candidate commit. The implementation contains no default successful
verifier and no wire/CLI/environment route to enable one. A callback returning
true without checking evidence is a host misconfiguration, not certification.
The synthetic verifier in `tests/fixtures/real_adapters/stdio_host.py` is only
for disposable conformance workloads. Its passing tests are not a production
certificate or independent behavioral review.

| Policy | Discovery / new tools | State and execution |
| --- | --- | --- |
| off (default) | absent | denied |
| observe | absent | denied; host may record diagnostics separately |
| enforce, uncertified or unbound | absent | denied |
| enforce, independently certified and authorized | facet-specific | authorized operations only |

Authorization is checked on every access and subscription poll; exceptions,
unknown identities, non-boolean verdicts and revocation deny access. The
principal/project never comes from request arguments, `_meta`, clientInfo,
or a task ID. Stores reject mismatched scope even when reopened. Discovery
and new resource/catalog results have private cache scope and zero TTL.
A heartbeat is liveness evidence only; it cannot authorize tasks or approvals.

## Contract pins and provenance

`src/scholialang_mcp/wire_schemas/RECEIPT.json` records immutable revisions and
file hashes. Core and Tasks schemas are the exact previously audited upstream
bytes, used by the runtime and tests with definition-specific selectors and
network reference retrieval disabled:

- Core: `modelcontextprotocol/modelcontextprotocol@e76e9c572c6f2bfcb730357101acc90f2f802e02`.
- Tasks: `modelcontextprotocol/experimental-ext-tasks@9263312d11a682ac83f83fe84794d4627efd22f5`.
- Portable Heartbeat: source revision `cb87379bbe4e26a8e59bab9cc18dc51ef4079bff`,
  package `0.1.0`, lease contract `0.1`. Its stdlib core is vendored privately
  with its MIT license and normative contract. Public normalization changes
  internal source-project names in prose, an example namespace, and the schema
  `$id`; the receipt retains both original and shipped hashes. Validation
  constraints and executable behavior are preserved.

The implementation uses the audited in-repository stdio layer and
`jsonschema==4.23.0`; it does not claim SDK Tasks runtime support. MCP protocol,
Tasks revision, lease version and Scholia/package version remain separate axes.

## Events

`subscriptions/listen` accepts core notification filters and acknowledges
only supported requested filters with
`notifications/subscriptions/acknowledged` as the first message for that
subscription. All stream notifications and graceful closing results carry
`_meta["io.modelcontextprotocol/subscriptionId"]` equal to the originating
JSON-RPC request ID. Numeric and string IDs remain distinct. A connection has
at most 32 subscriptions, each with at most 64 resource URIs and 64 task IDs.

The supported filters are `toolsListChanged`, authorized heartbeat
`resourceSubscriptions`, and authorized `taskIds`. Unsupported prompt/resource
catalog filters are omitted. Task notifications require the Tasks extension
on that listen request; resource hints require the Heartbeat extension.
Tasks and Heartbeat remain independently pollable when Events is off.

Hosts call `events.publish_tools_changed()` after changing their catalog.
Task state writes and heartbeat renewals commit notification intent in the
same store transaction. The transport polls committed intents while stdin is
idle. Each connection keeps its own bounded cursor, so delivery by one session
does not consume another session's events. Multiple notifications may reflect
one current task snapshot; neither transition-complete history nor exactly-once
delivery is promised.

Client `notifications/cancelled` closes only the referenced subscription and
has no response. It does not cancel tasks. EOF gracefully closes remaining
subscriptions. Authority loss and retention overflow also close streams;
clients resubscribe, then authoritatively refetch resources/tasks. There are no
standard replay, durable event-ack, or cursor RPCs. The store's delivery flags
and revisions stay internal.

## Tasks

The host registers tool definitions eligible for asynchronous processing.
Atlas tools remain synchronous. A registered tool returns a flat
`resultType: "task"` handle only after its scoped queue record, notification
intent and retry receipt commit. A client must declare
`extensions["io.modelcontextprotocol/tasks"]: {}` on every task request.
Missing support returns `-32021` with `requiredCapabilities` error data.
Legacy or unsupported eras cannot activate these methods.

The host owns workers and their execution/effect evidence; this package does
not ship a workload scheduler. Workers read authorized queue records, use
`tasks.require_input()` for pinned MRTR-shaped input requests, and call
`tasks.finish(result=...)`, `tasks.finish(error=...)`, or
`tasks.finish(cancelled=True)` when they have the corresponding execution
evidence. Input capabilities are captured with task creation and checked before
requesting input. A wire input response never dispatches a tool or grants a
separate approval by itself.

`tasks/get` returns flat detailed state, with `inputRequests`, `result`, or
`error` as appropriate. `tasks/update` validates keyed responses against both
the pinned response shape and the outstanding request's form schema; partial
input stays `input_required`. Identical responses are idempotent through the
task's retained lifetime, including after completion. Changed retries and
unknown keys fail closed without partial writes. Rejecting unknown keys is a
stricter application choice than the normative SHOULD-ignore recommendation.
Input keys cannot be reused, and terminal outcomes cannot be replaced.

`tasks/cancel` records cooperative cancellation intent and returns a complete
empty acknowledgement. It does not manufacture a terminal status. A worker
can subsequently cancel or finish successfully; `isError: true` tool results
still map to `completed`, while JSON-RPC execution errors map to `failed`.
Original-request MRTR `requestState`/`inputResponses` are rejected for these
registered task tools; task input goes through `tasks/update`.

The optional application metadata key `org.scholialang/idempotencyKey`
provides creation retry identity. It is not a standard MCP field. Its digest
is scoped by trusted principal and project, distinct from the JSON-RPC ID.
Reusing a token for different tool arguments fails. Without that key, each
invocation is a new task; JSON-RPC request IDs are not idempotency keys.
Concurrent processes resolve identical creates to one durable task. External
run, DAG, approval or inter-agent IDs never become MCP task identities.
Creation retries return a current handle; `tasks/get` remains authoritative.
Retention bounds limit retry guarantees; updates preserve the original expiry.

## Heartbeat

The existing vendor namespace is `com.dougfirlabs/heartbeat`. Discovery gives
`heartbeat://participants/{participant_id}` with percent-encoded participant
IDs and a maximum 30-second lease. `resources/list` describes the configured
participant and `resources/read` returns its authoritative six-field JSON
lease. No heartbeat RPC or standards-body heartbeat namespace is invented.

The trusted host calls `heartbeat.renew()` on its own schedule. Each adapter
instance mints a random boot epoch; clients reconnecting to a new publisher
must refetch that epoch. Old-process, malformed, expired, future-issued or
misaddressed leases fail closed. Renewal scheduling is host-owned and should
use monotonic time; wire instants are UTC. The reused portable core also exposes
its normative consumer lineage checks (duplicate, rollback, conflict, retired
epoch, skew and expiry). Consumer lineage persistence/scheduling is host-owned;
the server is a publisher, not a relay or remote heartbeat consumer.

`identity_binding` is advertised false: this local publisher does not produce
the portable current adapter's per-response transport identity assertion.
Configured local scope is enforced but is not represented as proof of an
upstream origin behind a gateway. Change hints are offered only when Events
is independently enabled, and merely prompt an authoritative lease refetch.

## Verification and limits

`tests/integration/test_real_adapters.py` launches the shipped adapter and
stdio loop in real subprocesses with synthetic workloads, writes complete
client/server captures for each process, and checks responses against pinned
schemas. It covers eight capability combinations, 27 uncertified policy
combinations, off/observe with a synthetic certificate, keyed input conflicts,
concurrent creates, cancellation races, reconnect/refetch, stream bounds,
retention overflow, forged identity, revocation and heartbeat lineage/expiry.
`tests/integration/` also gates the existing store, modern/legacy wheel, plugin
and LSP behavior. Captures are test artifacts, never operator trace databases.

The independent schema oracle is an additional schema-level check, not an
independent behavioral certification of this candidate. Production activation,
plugin propagation, hosted transport, and independent consumer integration
acceptance remain outside this adapter change. Rollback removes the embedding
host's injected configuration or reverts the adapter commit; all defaults stay
inert and no existing DAG database is migrated.
