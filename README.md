# scholialang-mcp

`scholialang-mcp` provides protocol tooling for Scholia:

- an MCP server exposing Scholia atlas lookup tools over stdio
- an MVP LSP server for editor navigation in `.scholia` traces
- provider stubs for Claude, Codex, Ollama, and OpenAI host adapters
- a local Codex plugin for SQLite-backed Scholialang DAG traces and full Codex
  rollout exhaust imports

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

## Codex Plugin

The release-ready Codex plugin lives at:

```text
plugins/codex/scholialang/
```

It bundles its own stdio MCP server, Codex skill, local SQLite trace store, and
Codex rollout exhaust importer. It is separate from the host-neutral atlas MCP
server above because it is packaged as a Codex plugin and exposes local trace
workflow tools such as `scholia.dag_start`, `scholia.dag_add_atom`,
`scholia.dag_export`, and `scholia.codex_import_thread`.

Install it from this repository:

```sh
codex plugin marketplace add "$(pwd)"
codex plugin add scholialang@scholialang-mcp
```

Start a new Codex thread after installation so Codex loads the plugin skill and
MCP tools.

The plugin stores traces locally at `~/.scholialang/scholialang.sqlite3` by
default. Set `SCHOLIALANG_HOME` before launching Codex to use a different
storage root.

See `plugins/codex/scholialang/README.md` for the full tool list, safety model,
and release validation commands.

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
