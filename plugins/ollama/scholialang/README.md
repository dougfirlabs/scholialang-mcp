# Scholialang Ollama / Local-Model Integration

Ollama is an inference server, not a plugin host. This tree ships the
Scholialang MCP server alongside recipe snippets for the most common
Ollama-backed coding harnesses that *do* speak MCP. The MCP server itself
is identical to the one shipped in the Codex and Claude Code plugins —
swap harnesses without losing the local DAG, validator, or import
surfaces.

## What This Tree Ships

- `scripts/scholialang_mcp_server.py` — the same stdio MCP server used by
  the Codex and Claude Code plugins. Local SQLite-backed DAG storage at
  `~/.scholialang/scholialang.sqlite3`. Full v0.6 grammar validator
  (`scholia_lint_snippet`, `scholia_lint_trace`).
- `scripts/_scholia_vendored/` — vendored validator/parser/atoms snapshot
  used when `pip install scholialang` is not available in the host
  environment.
- `recipes/` — drop-in configuration snippets for Continue.dev, Cline,
  open-webui, and a generic stdio MCP host. Each recipe wires the
  Scholialang MCP server into the harness so that an Ollama-backed model
  can call its tools.

## Wire Path

```
Ollama (inference) <-- HTTP --> Harness (Continue / Cline / open-webui / ...)
                                    ^
                                    | stdio (MCP)
                                    v
                                Scholialang MCP server (this tree)
                                    ^
                                    | SQLite
                                    v
                                ~/.scholialang/scholialang.sqlite3
```

The harness is the only component that needs to speak both Ollama
(model inference) and MCP (tool calls). The Scholialang server doesn't
care which model is producing the calls — Llama 3.3, Qwen, Codestral,
Mistral, Gemma, or anything else served by Ollama all work.

## Quick Start

1. Make sure Ollama is running (`ollama serve` or the desktop app).
2. Pick a harness from `recipes/` based on what you already use.
3. Drop the recipe's config snippet into the harness's config file
   (paths documented per recipe).
4. Open a new harness session — MCP servers load at session start.
5. Ask the model: *"Start a Scholialang trace for this project."*

There is no curl installer for this tree. Use the recipe snippets to wire the
bundled stdio server into your harness; install the Python package only when
you want the standalone atlas/LSP package or are developing the package itself.

For default, per-project **auto-emit** (no need to ask each session), paste
`recipes/autoemit-system-prompt.md` into your harness's system prompt. The model
then opens/resumes a session trace via the idempotent `scholia_dag_ensure_session`
and appends atoms as it works. Opt out with `SCHOLIA_AUTOEMIT=0` or a
`.scholia-off` file in the project root.

## Recipes

| Harness | File | Notes |
| --- | --- | --- |
| Continue.dev | `recipes/continue-config.snippet.yaml` | Add to `~/.continue/config.yaml` under `mcpServers:`. |
| Cline (VS Code) | `recipes/cline-mcp.snippet.json` | Add to `cline_mcp_settings.json`. |
| open-webui | `recipes/open-webui-mcp.snippet.json` | Add to the open-webui MCP config. |
| Generic stdio MCP host | `recipes/generic-stdio.md` | Use this if your harness speaks MCP over stdio but isn't listed. |
| Auto-emit system prompt | `recipes/autoemit-system-prompt.md` | Paste into the harness system prompt to enable default per-project auto-emit. |

## Validation Engine

The MCP server prefers the installed `scholialang` Python package and
falls back to `scripts/_scholia_vendored/` if the package isn't present.
Check `scholia_catalog`'s `lint_engine` field to confirm which path is
active:

- `scholialang-package` — installed pip package
- `scholialang-vendored` — bundled snapshot

To force the package path, `pip install scholialang>=0.5.0` in the
environment the harness uses to spawn the MCP server.

## Storage

By default, the local SQLite database lives at
`~/.scholialang/scholialang.sqlite3` and is shared across all three
plugins (Codex, Claude Code, Ollama-host). Traces captured during an
Ollama-harness session are visible from Codex and Claude Code sessions
and vice versa.

Set `SCHOLIALANG_HOME` in the harness's environment to override the
storage root.

### Project-Local Storage

For project work, point the harness at a repository-local database so
working state stays inside the checkout:

```sh
cd /path/to/project
export SCHOLIALANG_HOME="$PWD/.scholialang"
# then launch your harness (Continue, Cline, open-webui, etc.)
```

That stores the working trace database at:

```text
/path/to/project/.scholialang/scholialang.sqlite3
```

Recommended `.gitignore` entries:

```gitignore
.scholialang/*.sqlite3
.scholialang/*.sqlite3-*
.scholialang/exports/
```

Project-local storage silos traces per repo. To keep them cross-visible
with Codex or Claude Code on the same project, point those harnesses at
the same `SCHOLIALANG_HOME`. Commit curated SRML or Markdown summaries
only after review; keep raw exhaust imports local unless the repo is
private and reviewed for sensitive content.

## Safety Model

Same as the Codex and Claude Code plugins: traces are user-facing
artifacts (not hidden chain-of-thought), sensitive-looking text is
omitted, and encrypted reasoning is recorded by hash/length only.

## Validation

```sh
python3 -m py_compile plugins/ollama/scholialang/scripts/scholialang_mcp_server.py
python3 -m unittest plugins.ollama.scholialang.tests.test_scholialang_mcp_server
```
