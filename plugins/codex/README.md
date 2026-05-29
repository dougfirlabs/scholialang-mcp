# Codex Plugins

This directory contains Codex-specific integrations for `scholialang-mcp`.

## Scholialang

`plugins/codex/scholialang` is a local Codex plugin that bundles:

- a stdio MCP server for local Scholialang DAG traces
- a Codex skill for trace capture and retrieval
- SQLite-backed trace storage
- Codex rollout exhaust import and SRML export tools

Install from the repository root:

```sh
codex plugin marketplace add "$(pwd)"
codex plugin add scholialang@scholialang-mcp
```

The plugin is local-first and stores trace data under `~/.scholialang` unless
`SCHOLIALANG_HOME` is set before Codex launches.
