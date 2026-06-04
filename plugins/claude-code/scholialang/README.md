# Scholialang Claude Code Plugin

Local Claude Code plugin for explicit Scholialang traces, SQLite-backed DAG
storage, full v0.5 grammar validation, and rollout exhaust imports.

This plugin is intentionally local-first. The MCP server runs over stdio inside
Claude Code, writes to a local SQLite database, and does not require a hosted
service, bearer token, webhook, or remote worker.

## What It Ships

- `scholia_dag_*` tools for project-aware Scholialang DAG traces.
- `scholia_trace_*` compatibility aliases for trace-oriented callers.
- `scholia_catalog`, `scholia_lookup` reference tools (full v0.5 closed-set
  atom kinds, canonical operators, edge types, effect kinds, ref types, and
  criticality ladder).
- `scholia_lint_snippet` — full v0.5 grammar validation (closed-set atoms,
  reference completeness, decision closure, action recording, hypothesis
  evaluation, retract consistency, constraint respect, goal declaration,
  operator vocabulary, location/edge shape, Concluding closure errors, and
  warning checks). Pass `mode='tag_balance'` for the legacy tag-only check.
- `scholia_lint_trace` — per-rule structured error and warning output for CI
  gates and dashboards.
- `scholia_codex_import_thread` for importing Codex rollout JSONL into a
  durable exhaust DAG (cross-harness retro-analysis).
- A Claude Code skill that teaches the agent when to capture, validate,
  compact, search, and export Scholialang traces.

## Storage

By default, trace data is stored locally in SQLite under the user's home
directory:

```text
~/.scholialang/scholialang.sqlite3
```

Set `SCHOLIALANG_HOME` before launching Claude Code to use a different
storage root. Exports are written under:

```text
~/.scholialang/exports/
```

### Project-Local Storage

For day-to-day project work, prefer storing the working Scholialang
database inside the repository checkout and keeping it out of Git:

```sh
cd /path/to/project
export SCHOLIALANG_HOME="$PWD/.scholialang"
claude
```

With that setup, the plugin stores traces at:

```text
/path/to/project/.scholialang/scholialang.sqlite3
```

Recommended `.gitignore` entries:

```gitignore
.scholialang/*.sqlite3
.scholialang/*.sqlite3-*
.scholialang/exports/
```

Use the local SQLite database as working memory. Commit only curated
SRML or Markdown trace artifacts after review, usually under a
project-owned directory such as `scholia/traces/`. Raw full rollout or
exhaust imports should stay local unless the repository is private and
the trace has been reviewed for sensitive tool output.

## Install From This Repository

Inside Claude Code:

```text
/plugin marketplace add /path/to/scholialang-mcp
/plugin install scholialang@scholialang-mcp
```

Restart Claude Code (or open a new session) after installing — plugin skills
and MCP tools load at session start.

## Smoke Test

Ask Claude Code:

```text
Use Scholialang to start a local DAG for this project, add a hypothesis,
observation, evidence, and finding, then summarize the frontier.
```

Expected: a new local DAG is created in SQLite, four atoms added, the
frontier summary returns the final finding.

For the validator surface:

```text
Lint this Scholia snippet with the full v0.5 grammar:
<Step id="S1"><Hypothesis id="H1">x</Hypothesis></Step>
```

Expected: `lint_snippet` returns `ok=false` with the `goal_declared` and
`hypothesis_evaluated` rule violations populated.

## Validation Engine

The plugin prefers the installed `scholialang` Python package (kept in
lockstep with the spec). If unavailable, it falls back to the vendored
validator snapshot at `scripts/_scholia_vendored/`. Inspect the
`lint_engine` field returned by `scholia_catalog` or `scholia_lint_snippet`
to see which engine is in use:

- `scholialang-package` — the installed pip package
- `scholialang-vendored` — the bundled fallback

To force the package path, `pip install scholialang>=0.5.0` and restart
Claude Code.

## Safety Model

Scholialang traces are user-facing artifacts, not hidden chain-of-thought.

The importer records visible user, assistant, and tool text with configurable
truncation. It records encrypted reasoning and hidden reasoning references by
length/hash/reference metadata only. It does not materialize private
chain-of-thought.

Sensitive-looking text such as bearer tokens, API keys, passwords, private
keys, and access tokens is omitted from copied content and retained only as
length/hash metadata.

## Cross-Plugin Consistency

The Codex, Claude Code, and Ollama plugins in this repo all ship the
same MCP server script and the same vendored validator snapshot.
Switching between plugins does not change the tool surface, the storage
schema, or the validator semantics.

When every plugin uses the default `~/.scholialang/` storage root, the
local SQLite database is shared — traces captured in Codex are visible
from Claude Code and vice versa. Under project-local storage
(`SCHOLIALANG_HOME="$PWD/.scholialang"`), traces are siloed per repo
unless every harness in that project points at the same
`SCHOLIALANG_HOME`.

## Validation

Run from the repository root:

```sh
python3 -m py_compile plugins/claude-code/scholialang/scripts/scholialang_mcp_server.py
python3 -m unittest plugins.claude-code.scholialang.tests.test_scholialang_mcp_server
```
