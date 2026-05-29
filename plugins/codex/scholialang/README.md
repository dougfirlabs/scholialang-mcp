# Scholialang Codex Plugin

Local Codex plugin for explicit Scholialang traces, SQLite-backed DAG storage,
and full Codex rollout exhaust imports.

This plugin is intentionally local-first. The MCP server runs over stdio inside
Codex, writes to a local SQLite database, and does not require a hosted service,
bearer token, webhook, or Cloudflare worker.

## What It Ships

- `scholia.dag_*` tools for project-aware Scholialang DAG traces.
- `scholia.trace_*` compatibility aliases for trace-oriented callers.
- `scholia.catalog`, `scholia.lookup`, and `scholia.lint_snippet` reference
  tools.
- `scholia.codex_import_thread` for importing Codex rollout JSONL into a
  durable exhaust DAG.
- standalone HTML trace review exports via `scholia.dag_export`.
- A Codex skill that teaches agents when to capture, compact, search, and export
  Scholialang traces.

## Storage

By default, trace data is stored locally in SQLite under the user's home
directory:

```text
~/.scholialang/scholialang.sqlite3
```

Set `SCHOLIALANG_HOME` before launching Codex to use a different storage root.
Exports are written under:

```text
~/.scholialang/exports/
```

### Project-Local Storage

For day-to-day project work, prefer storing the working Scholialang database
inside the repository checkout and keeping it out of Git:

```sh
cd /path/to/project
export SCHOLIALANG_HOME="$PWD/.scholialang"
codex
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

Use the local SQLite database as working memory. Commit only curated SRML or
Markdown trace artifacts after review, usually under a project-owned directory
such as `scholia/traces/`. Raw full Codex exhaust imports should stay local
unless the repository is private and the trace has been reviewed for sensitive
tool output.

## Install From This Repository

From the repository root:

```sh
codex plugin marketplace add "$(pwd)"
codex plugin add scholialang@scholialang-mcp
```

Start a new Codex thread after installing. Codex loads plugin skills and MCP
tools at thread start, so already-open threads may keep an older loaded tool
set.

## Smoke Test

Ask Codex:

```text
Use Scholialang to start a local DAG for this project, add a hypothesis, observation, evidence, and finding, then summarize the frontier.
```

Expected result: a new local DAG is created in SQLite, at least four atoms are
added, and the frontier summary returns the final finding.

## Codex Exhaust Import

Use `scholia.codex_import_thread` to convert a Codex rollout JSONL into an
event-sourced Scholialang DAG.

The importer stores the full observable exhaust trail:

- one raw atom per observable Codex rollout event
- source file and line references
- SHA-256 hashes for provenance
- parse errors preserved as trace atoms
- tool result links back to tool call atoms

It also derives OpenTalon-compatible canonical envelopes from raw Codex events:

- `task_message`
- `task_tool_call`
- `task_tool_result`
- `token_usage`
- `task_output`

Codex CLI `command_execution` frames are normalized the same way as
OpenTalon's stage parser: a synthetic bash call, a synthetic bash result, and
the original raw frame preserved as `task_output`.

Typical input:

```json
{
  "project_path": "/path/to/project",
  "thread_id": "019e...",
  "max_events": 2000,
  "max_content_chars": 2000,
  "include_canonical_events": true
}
```

Use the raw exhaust DAG for audit/provenance. For human reading, generate a
deduped semantic view or compact summary so repeated user/assistant surfaces do
not look like repeated conversational turns.

## Session Defaults

The plugin should be quiet during ordinary Codex work:

- no automatic full exhaust import
- no automatic trace review export
- no automatic trace link in every response
- bounded project recall through summaries, search, frontier, and neighborhoods
- concise atom appends at meaningful work boundaries

Use full exhaust import only when the user asks for audit/provenance, token
analysis, or debugging of Codex behavior.

## Lightweight Trace Review UI

`scholia.dag_export` can produce a standalone HTML trace viewer. This is
default-off and should be requested explicitly. The viewer uses a local,
shadcn-style interface with searchable atom cards, kind filters, edge review,
and a highlighted full SRML tab.

```json
{
  "dag_id": "dag_...",
  "project_path": "/path/to/project",
  "format": "html",
  "write_file": true,
  "include_trace_link": false
}
```

With project-local storage, HTML exports are written under:

```text
/path/to/project/.scholialang/exports/
```

Set `include_trace_link` to `true` only when the user wants Codex to surface the
local export path in chat.

## Safety Model

Scholialang traces are user-facing artifacts, not hidden chain-of-thought.

The importer records visible user, assistant, and tool text with configurable
truncation. It records encrypted reasoning and hidden reasoning references by
length/hash/reference metadata only. It does not materialize private
chain-of-thought.

Sensitive-looking text such as bearer tokens, API keys, passwords, private keys,
and access tokens is omitted from copied content and retained only as
length/hash metadata.

## Validation

Run from the repository root:

```sh
python3 -m py_compile plugins/codex/scholialang/scripts/scholialang_mcp_server.py
python3 -m unittest plugins.codex.scholialang.tests.test_scholialang_mcp_server
python3 /path/to/plugin-creator/scripts/validate_plugin.py plugins/codex/scholialang
```

The first release candidate was dogfooded against a live Codex rollout and
verified to import both raw events and canonical OpenTalon-style events into
SQLite-backed Scholialang DAGs.
