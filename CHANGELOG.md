# Changelog

## Unreleased

- Add the local Codex Scholialang plugin under `plugins/codex/scholialang`.
- Add a repo-local Codex marketplace entry for `scholialang@scholialang-mcp`.
- Add full Codex rollout exhaust import into SQLite-backed Scholialang DAGs,
  including raw rollout atoms and OpenTalon-compatible canonical event atoms.
- Add standalone HTML trace review exports behind explicit `dag_export` options,
  including a searchable viewer and highlighted full SRML tab.
- Document the plugin storage model, safety policy, install flow, and validation
  commands.
- Document best practices for quiet Scholialang use during Codex sessions.

## v0.4.0

- Initial standalone protocol-tooling release for Scholia.
- Includes the MCP atlas server, provider adapter stubs, and the MVP LSP server.
