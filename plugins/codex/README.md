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

For project-local traces, launch Codex from the repository with a local storage
root:

```sh
cd /path/to/project
export SCHOLIALANG_HOME="$PWD/.scholialang"
codex
```

Ignore the working SQLite database and generated exports by default:

```gitignore
.scholialang/*.sqlite3
.scholialang/*.sqlite3-*
.scholialang/exports/
```

If a trace should become part of the project history, commit a curated SRML or
Markdown export outside the ignored working store after reviewing it for
sensitive tool output.
