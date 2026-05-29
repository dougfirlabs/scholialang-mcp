# scholialang-mcp

`scholialang-mcp` provides protocol tooling for Scholia:

- an MCP server exposing Scholia atlas lookup tools over stdio
- an MVP LSP server for editor navigation in `.scholia` traces
- provider stubs for Claude, Codex, Ollama, and OpenAI host adapters
- **three release-ready plugins** for the major coding harnesses, each
  with the same stdio MCP server, the same SQLite-backed local DAG,
  the same full v0.4 grammar validator, and shared storage:
  - `plugins/codex/scholialang/` — Codex plugin
  - `plugins/claude-code/scholialang/` — Claude Code plugin
  - `plugins/ollama/scholialang/` — Ollama / local-model recipes for
    Continue.dev, Cline, open-webui, and generic stdio hosts

The repo is intentionally separate from `scholialang`, which contains the
language model, parser, validator, and serializers. This package depends on
`scholialang>=0.4.0` and tracks `scholialang-spec` v0.4.0.

## Install

```sh
pip install scholialang-mcp
```

For local development:

```sh
python -m venv .venv
. .venv/bin/activate
pip install -e ".[dev]"
pytest
```

## MCP Server

Run the MCP server against a workspace root:

```sh
python -m scholialang_mcp --repo-root /path/to/repo
```

The server speaks JSON-RPC over stdio and supports the MCP handshake:
`initialize`, `tools/list`, and `tools/call`.

Tools:

- `lookup_file_summary(path)`
- `lookup_directory_summary(path)`
- `lookup_feature_summary(feature)`
- `lookup_kb_summary(path)`
- `lookup_prd_summary(path)`
- `lookup_doc_summary(path)`
- `get_tree()`
- `regenerate(path)`

Artifacts are read from a generic `.scholia-atlas/` directory when present.
Missing artifacts return structured `not_generated_yet` responses so host
agents can fall back to ordinary file reads. Regeneration is host-specific in
v0.4 and returns `regenerate_unavailable` unless a host adapter enables it.

### Codex Global MCP Snippet

To expose the server globally to Codex, add the snippet printed by:

```sh
python -m scholialang_mcp codex-config --repo-root /path/to/repo
```

The command does not edit user config; it prints the `[mcp_servers]` section so
installers and host-specific packages can apply it with explicit user consent.

## Harness Plugins

Three release-ready plugin trees ship with this repo, one per major
coding harness. Each plugin bundles the same stdio MCP server, the same
local SQLite DAG store, the same full v0.4 grammar validator, and the
same Codex rollout importer. Traces written in one harness are visible
from the other two (shared `~/.scholialang/scholialang.sqlite3`).

| Harness | Tree | Install |
| --- | --- | --- |
| Codex | `plugins/codex/scholialang/` | `codex plugin marketplace add "$(pwd)"` then `codex plugin add scholialang@scholialang-mcp` |
| Claude Code | `plugins/claude-code/scholialang/` | `/plugin marketplace add /path/to/scholialang-mcp` then `/plugin install scholialang@scholialang-mcp` inside Claude Code |
| Ollama (Continue / Cline / open-webui / generic stdio) | `plugins/ollama/scholialang/` | Drop a snippet from `recipes/` into your harness config |

Each plugin's tool surface is identical:

- `scholia.dag_*` — local SQLite DAG traces
- `scholia.trace_*` — compatibility aliases
- `scholia.catalog`, `scholia.lookup` — reference lookups across the
  full v0.4 closed-set vocabulary (31 atom kinds, 11 canonical
  operators, v0.3.1 edge/effect/ref types, v0.4-B edge types)
- `scholia.lint_snippet` — full v0.4 grammar validation (closed-set
  atoms, reference completeness, decision closure, action recording,
  hypothesis evaluation, retract consistency, constraint respect, goal
  declaration, operator vocabulary, location/edge shape). Pass
  `mode='tag_balance'` for the legacy tag-only check.
- `scholia.lint_trace` — per-rule structured error output for CI gates
  and dashboards
- `scholia.codex_import_thread` — import Codex rollout JSONL as an
  event-sourced exhaust DAG

The validator prefers the installed `scholialang` Python package and
falls back to the vendored snapshot at
`<plugin>/scripts/_scholia_vendored/`. Check the `lint_engine` field
returned by `scholia.catalog` to see which engine is active.

See each plugin's `README.md` for harness-specific install instructions,
safety model, and release validation commands.

## LSP Server

Run the LSP server:

```sh
python -m scholialang_mcp.lsp --workspace-root /path/to/repo
```

MVP v0.4 scope:

- Hover over `location="path:start:end"` attributes and show the referenced
  source span.
- `textDocument/definition` for resolvable `<Edge target="...">` and
  `<Ref target="...">` values.
- Definition resolution order: workspace-relative file path,
  `path.py::symbol` path prefix, then Scholia atom id in the current document
  when that atom has a `location` attribute.

Deferred past v0.4:

- completions
- diagnostics as you type
- find-all-references
- document symbols
- rename refactor
- full Python import resolution without an atlas or reverse-index artifact

Editor wiring uses the normal stdio LSP shape. VS Code, Neovim, and Emacs
adapters should launch `scholialang-lsp --workspace-root <repo>`.
