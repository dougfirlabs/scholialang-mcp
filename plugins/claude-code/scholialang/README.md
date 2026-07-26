# Scholialang Claude Code Plugin

Local Claude Code plugin for explicit Scholialang traces, SQLite-backed DAG
storage, full v0.6 grammar validation, and rollout exhaust imports.

This plugin is intentionally local-first. The MCP server runs over stdio inside
Claude Code, writes to a local SQLite database, and does not require a hosted
service, bearer token, webhook, or remote worker.

## What It Ships

- `scholia_dag_*` tools for project-aware Scholialang DAG traces.
- `scholia_trace_*` compatibility aliases for trace-oriented callers.
- `scholia_catalog`, `scholia_lookup` reference tools (the v0.6 closed-set
  atom kinds, canonical operators, edge types, effect kinds, ref types, and
  criticality ladder).
- `scholia_lint_snippet` — full v0.6 grammar validation (closed-set atoms,
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

## Scholia Live (web viewer)

An optional local web dashboard that shows this project's session DAG and
streams new atoms live (Server-Sent Events). It is **off by default** and
reuses the existing per-project SQLite store — no hosted service.

Enable it with `SCHOLIA_LIVE` (in your Claude Code `settings.json` `env` block,
or your shell):

```text
SCHOLIA_LIVE=1               # 1 / true / on / yes
SCHOLIA_LIVE_PORT=8765       # optional; default 8765, falls back to the next free port
SCHOLIA_LIVE_SCOPE=project   # optional; "project" (default) or "all"
SCHOLIA_LIVE_RECENT_SECS=300 # optional; recency window for the "live" badge (seconds)
```

When enabled, the `SessionStart` hook launches a singleton stdlib HTTP server
bound to `127.0.0.1` and prints its URL:

```text
Scholia Live viewer: http://127.0.0.1:8765/?project_path=<cwd>
```

Open that URL in a browser. The page includes a Settings panel (gear icon,
bottom-right) that toggles per-project auto-emit (creating/removing
`.scholia-off`) and shows the active storage and database paths. The server is
read-only apart from that toggle.

### Multi-Project Switcher and Scope Toggle

The single shared server can show traces for any project that has traces, so one
viewer covers every open Claude Code project. The header has a scope toggle and a
project dropdown:

- **This project** (default) — show only the current project's DAGs. The project
  dropdown (entries read `name (dag_count)`, with a `●` badge on recently-active
  projects) lets you jump to one other single project, still scoped to it.
- **All projects** — blend DAGs across every project; the graph pane opens the
  most-recent trace and the live stream follows all projects.

Selecting a different project (or flipping the toggle) clears the current trace,
refetches the snapshot, reconnects the live stream, and writes the `scope` /
`project_path` into the URL. **URL params are authoritative on load**, so a
reload (or a launcher-supplied `?project_path=<cwd>`) restores exactly that view
— this fixes the stale-localStorage "stuck on one project" behavior.

Two env vars tune the defaults:

- `SCHOLIA_LIVE_SCOPE` — `project` (default) or `all`. Sets the scope a fresh tab
  opens in. Effective scope follows the precedence
  `?scope=` URL param > saved UI choice (localStorage) > `SCHOLIA_LIVE_SCOPE` >
  built-in `project`.
- `SCHOLIA_LIVE_RECENT_SECS` — recency window in seconds (default `300`) for the
  `●` "live" badge. A project is live when its newest session DAG updated within
  the window.

A single-project user who sets neither env var nor any UI/URL choice sees the
unchanged first-load behavior (their current project only).

The server is a singleton recorded in
`${SCHOLIALANG_HOME:-~/.scholialang}/live-server.json` and is shared across
sessions (the SQLite DB is shared). Stop it with:

```sh
kill "$(python3 -c 'import json,os;print(json.load(open(os.path.expanduser("~/.scholialang/live-server.json")))["pid"])')"
```

## Live exhaust capture (Checkpoint/Exhaust toggle)

The auto-emitted session DAG is a **curated checkpoint** trace. Alongside it, a
per-session **exhaust** trace is captured live: a mechanical, event-by-event
mirror of the Claude Code transcript (`~/.claude/projects/<slug>/<session>.jsonl`).
It is **on by default** (whenever auto-emit is on) and adds **zero LLM tokens** —
capture is an out-of-band parse of the transcript Claude Code already writes; it
makes no model or network calls and never injects into the agent's context.

It rides on the shared auto-emit opt-out, so `SCHOLIA_AUTOEMIT=0` or a
`.scholia-off` marker disables it along with checkpoint emission. To disable
*only* exhaust (keeping checkpoint), set `SCHOLIA_EXHAUST=0`:

```text
SCHOLIA_EXHAUST=0               # 0 / false / off / no  — disable exhaust only (on by default)
SCHOLIA_EXHAUST_MAX_EVENTS=2000 # optional; cap on captured transcript events (default 2000)
```

The `SessionStart` hook launches a detached background tailer that
follows the session transcript and appends exhaust atoms to a paired exhaust DAG
(tagged `exhaust` + `event-source` and titled `"<checkpoint title> — exhaust"`).
The Scholia Live viewer's existing **Checkpoint/Exhaust** toggle then pairs and
switches between the two traces for that session — no viewer changes required.
`SessionEnd` stops the tailer.

Capture honors the shared opt-out (`SCHOLIA_AUTOEMIT=0` or a `.scholia-off`
marker) and `SCHOLIALANG_HOME`. It is idempotent: each transcript line maps to a
stable per-line atom id, and the tailer resumes from the last imported line, so a
restart mid-session never duplicates atoms. With `SCHOLIA_EXHAUST=0` (or auto-emit
opted out), no exhaust DAG or tailer is created. All capture failures exit
0 — exhaust capture never breaks the session.

## Install From GitHub

Install the public marketplace and plugin:

```sh
claude plugin marketplace add https://github.com/dougfirlabs/scholialang-mcp.git --scope user
claude plugin install scholialang@scholialang-mcp --scope user
```

Use `--scope project` or `--scope local` instead when you want the marketplace
and plugin tied to one repository. Restart Claude Code (or open a new session)
after installing — plugin skills and MCP tools load at session start.

The Claude Code plugin is the recommended install path for Claude Code users.
It bundles the stdio MCP server, so it does not need a prior `pip install` or a
curl installer. Use `python -m pip install scholialang-mcp` only for the
standalone atlas/LSP package or package development.

Inside an existing Claude Code session, run:

```text
/reload-plugins
/mcp
```

Expected: `scholialang` appears in `/mcp` with the other connected servers.

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
Lint this Scholia snippet with the full v0.6 grammar:
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

To force the package path, `python -m pip install "scholialang>=0.6.2,<0.7"`
and restart Claude Code.

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
