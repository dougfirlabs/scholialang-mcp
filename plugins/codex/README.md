# Codex Plugins

This directory contains Codex-specific integrations for `scholialang-mcp`.

## Scholialang

`plugins/codex/scholialang` is a local Codex plugin that bundles:

- a stdio MCP server for local Scholialang DAG traces
- a Codex skill for trace capture and retrieval
- SQLite-backed trace storage
- Codex rollout exhaust import and SRML export tools

Install from the public GitHub marketplace:

```sh
codex plugin marketplace add https://github.com/dougfirlabs/scholialang-mcp.git
codex plugin add scholialang@scholialang-mcp
codex plugin list
```

The plugin install provides the Codex skill, marketplace metadata, and bundled
MCP configuration. Start a new Codex thread after installing so the
plugin-provided `scholia_*` tools load.

Do not install the Python package first for normal Codex usage. The plugin
launches its bundled stdio server directly; `python -m pip install
scholialang-mcp` is only for the standalone atlas/LSP package or local package
development.

If the plugin metadata loads but the `scholia_*` tools do not appear, use the
direct MCP registration as a troubleshooting fallback from a local checkout:

```sh
git clone https://github.com/dougfirlabs/scholialang-mcp.git
cd scholialang-mcp
codex mcp add scholialang \
  -- python3 "$PWD/plugins/codex/scholialang/scripts/scholialang_mcp_server.py"
```

The server is local-first and stores trace data under `~/.scholialang` unless
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
