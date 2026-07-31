# ADR 0001 — MCP 2026-07-28 dual-version adapter architecture

- **Status**: accepted (PRD `mcp-2026-07-28-prd-01-dual-version-adapter`, story MCP01-S1)
- **Date**: 2026-07-29
- **Context**: PRD-00 compatibility audit (internal report, 2026-07-28) — v0.6.2 FAILs MCP 2026-07-28 core conformance on both server surfaces while legacy stdio lifecycles (2024-11-05 … 2025-11-25) are fully functional.
- **Final-stable source**: upstream tag `2026-07-28`, commit
  `5f5440bb26a62e2cf3440b92da5a667efa03b267`.

## Decision

Keep the **small audited in-repo wire layer** and extend it into an explicit
dual-version adapter. Do **not** adopt the official MCP Python SDK for either
server surface. One canonical Scholia tool/domain implementation per surface
is exposed through a thin protocol layer that speaks both protocol
generations; version selection is per-request and fail-closed.

## Considered: official Python SDK vs. audited wire layer

| Dimension | Official `mcp` Python SDK | In-repo wire layer (chosen) |
|---|---|---|
| SDK maturity | Mature for the handshake era; 2026-07-28 stateless support is new surface still settling | ~150 lines of protocol code per surface, frozen with the spec revisions we claim |
| Conformance | Tracks the spec's *legal* behaviors, including the legacy silent counter-offer on `initialize` — which this PRD's hard constraint forbids | Fail-closed `-32022` on any declared-unsupported version is a deliberate, tested policy override (see below) |
| Dependency cost | Pulls `httpx`/`pydantic`/`anyio`-class deps into a package whose only transport is stdio; **cannot be vendored into the single-file stdlib-only plugin servers** that ship inside the claude-code/codex/ollama bundles | Zero new dependencies; the plugin server stays a single stdlib-only file that `sync_plugins.sh` copies verbatim |
| Transport support | Strong HTTP story we do not use (v0.6.x offers stdio only) | Exactly the stdio transport we ship; HTTP remains out of scope until a PRD claims it |
| Maintenance | Tracks upstream on their cadence; adapter behavior can shift under us between releases | We own the diff; PRD-00's probe suite + the conformance tests in `tests/integration/` are the audit trail |

The single-file vendoring requirement for the plugin bundles is on its own
decisive: the three host plugins must run with no pip-installable
dependencies, which excludes the SDK for the plugin surface — and splitting
the two surfaces across two protocol stacks would violate this PRD's
"no divergent semantics across adapters" constraint.

## Version-selection / discovery / legacy boundary

Protocol semantics are isolated from Scholia tool/domain logic in a thin,
named layer per surface; domain code never reads or writes protocol fields:

- **wheel server** (`src/scholialang_mcp/server.py`): `_ok` / `_err` /
  `_requested_protocol_version` / `_negotiated_protocol_version` /
  `_discover_result` / `_handle_request`. Tool functions receive plain
  arguments and return plain payloads.
- **plugin server** (`plugins/claude-code/scholialang/scripts/scholialang_mcp_server.py`,
  the canonical copy; codex/ollama copies are **generated** from it by
  `scripts/sync_plugins.sh` and byte-parity-gated by
  `tests/integration/test_mcp_plugin_2026_conformance.py`): `rpc_result` /
  `rpc_error` / `requested_protocol_version` / `negotiated_protocol_version` /
  the `server/discover` branch of `dispatch` / the fail-closed guard in
  `handle_message`.

Selection rules (identical on both surfaces):

1. `server/discover` is answered for a well-formed modern request and is the
   stdio era probe. A probe missing the required modern `_meta` receives
   `-32602`.
2. A request carrying `_meta["io.modelcontextprotocol/protocolVersion"]`
   (2026-07-28 carriage) declares that version; `_meta` **takes precedence**
   over the legacy `params.protocolVersion` field when both are present.
3. Any declared version outside the selected era's support table fails closed
   with final-stable `-32022 UnsupportedProtocolVersion`, including the
   `supported` and `requested` error data. This
   deliberately drops the legacy counter-offer negotiation (spec-legal
   pre-2026) because the PRD hard constraint "unsupported protocol versions
   fail explicitly rather than silently falling back" outranks it. Old hosts
   requesting any version the server actually supports are unaffected.
4. A request carrying modern `_meta` selects modern semantics. An `initialize`
   request without modern `_meta` selects legacy semantics; 2026-07-28 cannot
   be negotiated through that removed handshake.
5. Every result carries `resultType` + `_meta` serverInfo; list/read results
   additionally carry `ttlMs` + `cacheScope: "private"` (local single-operator
   servers; nothing may enter a shared cache).

The support table itself is per-surface (the wheel server supports
2024-11-05; the plugin server never did) and the published matrix is
**generated from live probes** — `docs/mcp-support-matrix.md` via
`tests/integration/test_mcp_support_matrix.py`.

Removed surface: 0.6.2's undocumented dispatch of unknown JSON-RPC methods as
direct tool invocations (audit G5, probe p7) is gone; unknown methods return
`-32601`, tools are reachable only through `tools/call`.

## Legacy deprecation policy (bounded)

- The legacy handshake lifecycle (`initialize`, `ping`, params-carried
  version) is supported for the **entire 0.7.x line**.
- Earliest removal is **0.8.0**, and only with a deprecation notice shipped in
  the 0.7.0 README + support matrix one minor line in advance.
- Removal (like any release/publish action under this epic) is
  operator-gated; nothing in this ADR authorizes it.

## Versioning

The remediation ships as **scholialang-mcp 0.7.0**. Package, MCP/LSP server,
plugin, and marketplace versions move together. The separately versioned
Scholia language, validator, conformance fixtures, and Python dependency remain
at **0.6.2**.

## Rollback

The adapter is wire-only: no Scholia language, validator, DAG/SQLite, or
atlas artifact format changed. Restoring the legacy-only adapter is a clean
revert:

```sh
# in scholialang-mcp, on the affected branch
git revert <adapter-commit-range>   # 9fa683c.. — the PRD-01 commits
scripts/sync_plugins.sh             # re-propagate the reverted plugin server
python -m pytest tests/integration/test_mcp_protocol.py tests/integration/test_lsp_protocol.py
```

Installed hosts roll back by pinning the previous artifacts: PyPI
`scholialang-mcp==0.6.2` (wheel sha256 `bcf1de8d…`) and the v0.6.2 plugin
bundles (vendored server sha256 `ed1c2bca…`, hashes recorded in PRD-00 §2).
No on-disk state migration is needed in either direction.
