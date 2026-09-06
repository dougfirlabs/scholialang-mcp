# ADR 0002 — Frozen MCP capability, identity and durable-state contracts

- **Status**: accepted (PRD `sch073-04-capability-contract-scholialang-mcp`, stories S1–S3)
- **Date**: 2026-09-05
- **Context**: package versions, semantic vocabulary and transport extensions
  can drift independently; nothing may be promoted on the strength of a name,
  a version number, an HTTP 200 or the presence of Event/Task atoms. The
  independent protocol reviewer's pins and reuse audit live at
  `.ralph/contracts/protocol-contract-pins.md` (read-only evidence); this ADR
  records the decisions this repository now enforces.

## Decision

Freeze an executable capability/identity contract, declared identically by
every server surface and validated by machine-runnable checks:

1. **Machine-readable manifest** — `contracts/mcp-capability-contract.v1.json`
   pins the authoritative revisions (MCP core `e76e9c57…`, Tasks extension
   `9263312d…`, Python SDK `0921d94a…`, OT heartbeat source `cb87379b…`, the
   upstream schema oracle hashes, and the independently accepted core 0.7.3
   wheel/sdist SHA256 receipts). Its `declaration` section is byte-equal (as
   canonical JSON) to `scholialang_mcp.capabilities.CAPABILITY_DECLARATION`
   and to the `CAPABILITY_DECLARATION` block in the three byte-identical
   plugin servers.
2. **Independent negotiation** — Events (core 2026-07-28 subscriptions),
   Tasks (`io.modelcontextprotocol/tasks`) and Heartbeat
   (`com.dougfirlabs/heartbeat` lease 0.1) negotiate independently. Support is
   never inferred from package name, version, transport availability, or a
   sibling facet. Facet support is per request era: legacy (pre-2026)
   requests never reach an active facet (`modern_protocol_required` refusal).
3. **Per-extension rollout policy** — `off`/`observe`/`enforce` from trusted
   environment configuration (`SCHOLIALANG_MCP_EXT_<FACET>_POLICY`), default
   `off`, unrecognized values fail closed to `off`. `observe` records but
   never executes, advertises, or approves. `enforce` activates only an
   implemented, independently conformance-certified adapter — a policy value
   can never manufacture support, and a downgrade can never falsely certify
   it.
4. **Shipped server advertises nothing unfinished** — the shipped adapter
   registry is empty, so discovery advertises no Events/Tasks/Heartbeat
   capability under any policy combination. The eight-combination support
   matrix is proven with synthetic adapters that mark themselves
   `synthetic: true` on the wire; mocked adapter parity is recorded as such
   and is not shipped transport support.
5. **Identity fail-closed** — principal + project bind from trusted launcher
   configuration only (stdio = configured local principal, not an
   authenticated network peer). Caller-supplied owner/project fields are
   never authoritative; unbound contexts fail closed (`-32000`); foreign and
   missing records are byte-identical (`-32001`) so there is no cross-tenant
   existence oracle. MCP task ids, OT run ids, A2A ids, approval ids and
   DAG/atom ids remain distinct axes.
6. **Durable-state seam** — `ScopedMemoryStore` fixes the promised semantics
   (single commit point, notifications-after-commit, per-scope revisions and
   bounded retention) as an interface + synthetic implementation. The
   authoritative durable store, fault-injection evidence and reconnect
   contract remain a **blocking** feature gap in the manifest.

## Reuse audit (S1)

Accepted for reuse: the OT `packages/mcp-heartbeat` portable core and
current/legacy adapters (vendor contract, no new namespace); GOAT's bounded
project-scoped event projections, cursor expiry and subscription-limit
patterns; GOAT `FileWorkStore` fsync/atomic-replace/flock persistence
technique; tasks-prototype ideas (journal rehydration without fabricated
completion, expiring hashed approval tokens, distinct task/run identity,
cooperative cancellation).

Rejected: the historical Tasks/MRTR prototype's wire implementation (legacy
`tasks/provide_input`/`tasks/result`, nested task result shape, `_meta`-read
augmentation, non-durable JSONL broker); `GoatTaskService`/`GoatEventLog` as
MCP wire services; the SDK v2.1.1 modern Tasks runtime (explicitly deferred
upstream) and legacy `mcp_types` task shapes.

## Version axes and unsupported combinations

Protocol, package, grammar and capability contract report independently:
package 0.7.2, grammar 0.6.2, vendored engine 0.7.2, accepted core candidate
0.7.3 (receipt-pinned, **not yet vendored** — `gap-core-073-vendoring`,
blocking, because the 0.7.3 engine moves grammar to 0.7.0 / 35 kinds and
must rebind the fingerprint and vendor-contract baselines explicitly, with
serializer + PyYAML dependency closure and license provenance, rather than
silently weakening them). The wheel surface accepts protocol 2024-11-05;
the plugin surface intentionally rejects it — preserved, not widened.
HTTP transports are unsupported on every surface.

## Verification

- `python -m pytest tests/integration/test_capability_contract.py --timeout=30`
  — fixture-driven live-wire matrix (8 support combos, 27 rollout modes,
  incompatible protocol, identity denial, malformed payloads, old-consumer
  arm, cross-surface declaration parity).
- `python3 scripts/capability_conformance.py [--json report.json]` — the
  shared machine-runnable conformance runner + manifest validator (73
  checks), reusable by the separately owned OT consumer against exact
  producer SHAs.
