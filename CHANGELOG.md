# Changelog

## Unreleased

- Add the local Codex Scholialang plugin under `plugins/codex/scholialang`.
- Add a repo-local Codex marketplace entry for `scholialang@scholialang-mcp`.
- Add full Codex rollout exhaust import into SQLite-backed Scholialang DAGs,
  including raw rollout atoms and internal agent harness canonical event atoms.
- Document the plugin storage model, safety policy, install flow, and validation
  commands.
- **Port the full scholialang v0.5 grammar validator into the bundled MCP
  server.** `scholia_lint_snippet` now runs the complete 17-rule check
  (well-formed, reference complete, decision closed, action recorded,
  hypothesis evaluated, retract consistent, constraint respected, goal
  declared, unknown operator, location/edge shape, v0.3.1 optional
  fields, Concluding goal resolution, Concluding citations, criticality
  non-decrease, action-modal warnings, duplicate active Concluding warnings,
  and confidence-ceiling warnings). The previous tag-balance behaviour is
  preserved via `mode='tag_balance'`. Validator prefers the installed
  `scholialang` package; falls back to a vendored snapshot at
  `scripts/_scholia_vendored/` when the package isn't available.
- Add `scholia_lint_trace` returning per-rule structured errors, warnings,
  and counts for CI gates and dashboards.
- Expand `scholia_catalog` to expose the full v0.5 closed-set
  vocabulary (32 atom kinds, canonical operators, edge types, effect
  kinds, ref types, and criticality rank) plus `lint_engine` and
  `validator_version`.
- **Add the Claude Code plugin under `plugins/claude-code/scholialang/`.**
  Same MCP server, same vendored validator, same skill semantics.
  Marketplace + Claude Code plugin manifest included.
- **Add the Ollama / local-model integration tree under
  `plugins/ollama/scholialang/`.** Same MCP server plus drop-in recipes
  for Continue.dev, Cline, open-webui, and a generic stdio MCP host
  spec. Single shared local SQLite store across all three plugins.
- Cross-plugin parity test guards against server-script drift between
  the three plugin trees.
- Bump bundled server version to `0.2.0`.

## v0.4.0

- Initial standalone protocol-tooling release for Scholia.
- Includes the MCP atlas server, provider adapter stubs, and the MVP LSP server.
