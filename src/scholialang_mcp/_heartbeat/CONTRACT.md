# MCP Heartbeat — extension_version 0.1

**Status: normative.** This document plus
`schema/mcp-heartbeat-0.1.schema.json` is the whole contract. A clean-room
implementer needs nothing else — in particular, no host-application source.

`extension_version` versions *this contract*. It is a separate axis from the
MCP protocol revision: the same 0.1 heartbeat is carried unchanged by a
2025-06-18 transport and a 2026-07-28 transport. Neither era appears here.

---

## 1. What a heartbeat is

> A heartbeat is a short-lived, ordered **liveness lease** published by one
> participant. It asserts exactly one thing: *this participant was alive and
> publishing at `issued_at`, and claims the window through `expires_at`.*

It does **not** assert readiness, health, consistency, load, authorization,
correctness, or task success, and it never will — those are projections a
consumer computes elsewhere from its own inputs. Freshness is not permission.
A consumer that reads a fresh heartbeat as a licence to dispatch work has made
an error this contract cannot prevent but does not invite.

## 2. The mandatory object

Six members, no more:

```json
{
  "extension_version": "0.1",
  "node_id": "svc/api-7",
  "boot_id": "3f2a91c0",
  "sequence": 12,
  "issued_at": "2026-01-01T00:00:00.000Z",
  "expires_at": "2026-01-01T00:00:30.000Z"
}
```

Optional data is permitted only under `extensions` (keys MUST be namespaced,
i.e. contain a dot) or as a namespaced top-level member. Such data is **safely
ignorable**: a conforming consumer that discards every optional member reaches
exactly the same freshness and lineage verdict. Optional data MUST NOT
participate in core validation. Any non-namespaced member the core does not
define is rejected, so a typo cannot masquerade as an extension.

## 3. Participant and epoch

The invariant everything else follows from:

> A **participant** is whatever publishes exactly one totally-ordered heartbeat
> stream at exactly one address. Sequence monotonicity is scoped to
> `(participant, epoch)`. An **epoch** is minted once per process start and is
> never reused, derived, or predicted.

At extension_version 0.1 the wire keys remain `node_id` and `boot_id`. This is
deliberate: the corpus, the schema, and deployed readers key on those names,
and the rename buys no semantic capability. *Participant* and *epoch* are the
normative prose terms and the names this package's API uses
(`Heartbeat.participant_id`, `Heartbeat.epoch_id`); the wire keys are their
serialization. A wire rename to `participant_id` / `epoch_id` is deferred to
extension_version 1.0, with a versioned corpus and an explicit migration.

### 3.1 Replicas

Each replica is its own participant: N replicas publish N streams under N
distinct `node_id`s. Sharing one `node_id` across replicas is a deployment
error, not a supported mode — two independent sequence streams under one
identity interleave, and a consumer correctly reports `sequence_conflict`.
"Is the service up" is an aggregate computed over N participants. It is not a
heartbeat field and MUST NOT become one.

### 3.2 Gateways

A gateway is a relay, never a participant. It MUST NOT rewrite, mint, or
substitute `node_id`, and MUST NOT re-sign a heartbeat. The principal a
consumer authenticates behind a gateway is *the gateway*, not the origin
participant; the identity binding is therefore `unverified` no matter how
strong the transport to the gateway is (§5).

### 3.3 Round-robin routing

A heartbeat address MUST resolve to one specific participant and MUST NOT be
load-balanced across replicas. A round-robined address delivers interleaved
frames from different epochs, which presents as epoch flapping and then as
`boot_id_reuse` or `sequence_conflict`. Deployments whose ingress cannot
guarantee participant-stable routing MUST front heartbeats with a
per-participant path or not publish them at all.

### 3.4 Cold starts

A new process is a new epoch, always. `boot_id` MUST be freshly minted at
process start from a random source. It MUST NOT be derived from hostname,
config, container name, deployment id, or a seed — a derived epoch re-presents
a retired identifier on restart, which a consumer correctly classifies as
replay. `sequence` resets with the epoch.

**Stated scope limit.** Heartbeats are unsuitable for participants whose
process lifetime is shorter than the lease window: such a participant publishes
a lease that expires unobserved, and a consumer cannot distinguish that from a
death. Represent those workloads by the invoking long-lived participant.

### 3.5 Rolling deployments

Old and new processes coexist by design; each holds its own epoch, so a
consumer observes the old epoch expire and the new one appear — a restart, not
a conflict.

- A rolling replacement that keeps the same `node_id` MUST mint a new `boot_id`.
- A **rollback** to a previously-running binary MUST also mint a fresh epoch.
  Reusing the prior epoch trips the retired-epoch set and is rejected — the
  consumer cannot distinguish a rollback from a replay, so it fails closed.
- During overlap both epochs may briefly be fresh at different addresses. That
  is normal and requires no reconciliation.
- Draining is not a heartbeat concern. This contract's answer to "is this
  participant going away" is only "its lease will expire".

## 4. Admission rules

A consumer holds, per participant, the last accepted heartbeat and the set of
retired epochs. Checks apply in this order, and the order is normative:

1. **Structure** — schema violations reject as `schema_invalid`; a declared
   version other than `0.1` rejects as `unsupported_extension_version`; a
   non-positive window rejects as `invalid_expiry_window`; a window longer than
   3600s rejects as `expiry_window_too_long`.
2. **Addressing** — a document whose `node_id` is not the expected participant
   rejects as `node_id_mismatch`. This is a routing check, not authentication.
3. **Lineage** — within the held epoch: a lower `sequence` is
   `sequence_rollback`; an equal `sequence` with an equal digest is a
   **duplicate** (idempotent redelivery, not a transition, not an error); an
   equal `sequence` with a different digest is `sequence_conflict`. A
   previously-retired epoch reappearing is `boot_id_reuse`.
4. **Skew** — `|issued_at - now|` beyond the consumer's bound rejects as
   `clock_skew_exceeded`. Lineage is checked *before* skew on purpose: a
   replayed old revision is also stale, and a skew-first order would report
   every replay as skew and hide the rollback that actually happened.
5. **Expiry** — a document already past `expires_at` on arrival rejects as
   `expired_on_arrival`.

Only after all five does the heartbeat become the held state and its epoch
join the retired set. Every rejection preserves the previously held state:
this contract fails closed.

## 5. Identity is claimed, never proven

> A participant identity is **not** authenticated merely because it appears in
> a heartbeat.

The core parses `node_id` as a **self-claim**. It validates the claim's syntax
and its continuity across epochs; it has no means to check who actually
published it, and it MUST NOT report that it did. `IdentityClaim.authenticated`
is `False` unconditionally and there is no core code path that sets it
otherwise.

Binding a claim to an authenticated principal is a **transport adapter's** job,
because only the adapter sees the credential. An adapter reports one of three
values, and a consumer MUST NOT collapse them to a boolean:

- `bound` — a principal was determined and is permitted to publish this
  participant id.
- `unbound` — a principal was determined and is **not** permitted to publish
  this participant id. This is a security event: fail closed.
- `unverified` — no principal could be determined (no transport auth, or a
  relay sits in the path). Not an error and not a promotion. This is the
  default, and it is where every gateway deployment lands.

An adapter that cannot determine a principal reports `unverified`. It MUST NOT
fall back to "authenticated because the socket was TLS". The mapping from
principal to permitted participant ids is deployment configuration supplied to
the adapter; the portable core never holds a policy table.

## 6. Transport seams

The core defines four ports (`mcp_heartbeat.ports`) and implements none of
them. Each is a `typing.Protocol`, so an adapter satisfies it structurally
without importing a transport into the core:

| Port | Direction | Contract |
| --- | --- | --- |
| `HeartbeatPublisher` | producer → transport | publish an authoritative document |
| `HeartbeatSource` | transport → consumer | fetch the authoritative document for a participant |
| `HintReceiver` | transport → consumer | deliver a `ChangeHint` |
| `IdentityBinder` | adapter → consumer | resolve an `IdentityClaim` to an `IdentityBinding` |

A `ChangeHint` is a *hint*, never state: it carries an address, a revision and
a digest, and its only legal effect is to make the consumer refetch. A consumer
that misses every hint MUST still converge, because its refetch deadline
derives from the held lease's own expiry. Adding a port never widens the
six-field object.

## 7. Dependencies

The package imports the Python standard library and nothing else — no
host-application, no goatlib, no scholialang, no web framework, no MCP SDK, no JSON
Schema validator. `tests/test_purity.py` proves this at the AST level and by
importing the package in a subprocess started with site-packages disabled.
