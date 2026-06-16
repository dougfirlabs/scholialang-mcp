# Scholialang Codex Plugin

Local Codex plugin for explicit Scholialang traces, SQLite-backed DAG storage,
and full Codex rollout exhaust imports.

This plugin is intentionally local-first. The MCP server runs over stdio inside
Codex, writes to a local SQLite database, and does not require a hosted service,
bearer token, webhook, or Cloudflare worker.

## What It Ships

- `scholia_dag_*` tools for project-aware Scholialang DAG traces.
- `scholia_trace_*` compatibility aliases for trace-oriented callers.
- `scholia_catalog`, `scholia_lookup`, and `scholia_lint_snippet` reference
  tools.
- `scholia_codex_import_thread` for importing Codex rollout JSONL into a
  durable exhaust DAG.
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

## Install From GitHub

Install the public marketplace and plugin:

```sh
codex plugin marketplace add https://github.com/dougfirlabs/scholialang-mcp.git
codex plugin add scholialang@scholialang-mcp
codex plugin list
```

The plugin install gives Codex the Scholialang skill, marketplace metadata, and
bundled MCP configuration. Start a new Codex thread after installing;
already-open threads may keep an older loaded tool set.

The Codex plugin is the recommended install path for Codex users. It does not
need a prior `pip install` or a curl installer; use `python -m pip install
scholialang-mcp` only for the standalone atlas/LSP package or package
development.

### Manual MCP Fallback

If a Codex thread loads the plugin but does not expose working `scholia_*`
tools, clone this repository and register the bundled server directly:

```sh
git clone https://github.com/dougfirlabs/scholialang-mcp.git
cd scholialang-mcp
codex mcp add scholialang \
  -- python3 "$PWD/plugins/codex/scholialang/scripts/scholialang_mcp_server.py"
```

To print the equivalent `~/.codex/config.toml` fallback block, use this from a
local repository checkout:

```sh
PYTHONPATH=src python3 -m scholialang_mcp codex-trace-config --repo-root "$(pwd)"
```

## Smoke Test

Ask Codex:

```text
Use Scholialang to start a local DAG for this project, add a hypothesis, observation, evidence, and finding, then summarize the frontier.
```

Expected result: a new local DAG is created in SQLite, at least four atoms are
added, and the frontier summary returns the final finding.

## Codex Exhaust Import

Use `scholia_codex_import_thread` to convert a Codex rollout JSONL into an
event-sourced Scholialang DAG.

The importer stores the full observable exhaust trail:

- one raw atom per observable Codex rollout event
- source file and line references
- SHA-256 hashes for provenance
- parse errors preserved as trace atoms
- tool result links back to tool call atoms

It also derives internal agent harness canonical envelopes from raw Codex events:

- `task_message`
- `task_tool_call`
- `task_tool_result`
- `token_usage`
- `task_output`

Codex CLI `command_execution` frames are normalized the same way as
the internal agent harness stage parser: a synthetic bash call, a synthetic bash result, and
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

## Live Exhaust Capture (Checkpoint/Exhaust toggle)

`scholia_codex_import_thread` (above) is a one-shot, run-it-by-hand importer.
Alongside it, Codex sessions also stream a **live** exhaust trace automatically:
a mechanical, event-by-event mirror of the Codex rollout JSONL that Codex already
writes under `CODEX_HOME`. It is **on by default** (whenever auto-emit is on) and
adds **zero LLM tokens** — capture is an out-of-band parse of the rollout file; it
makes no model or network calls and never injects into the agent's context.

Because Codex has no SessionStart/SessionEnd plugin hooks (unlike Claude Code),
there is no per-session place to launch a tailer. Instead, the Codex plugin's
MCP entrypoint (`codex_mcp_entry.py`, wired in `.mcp.json`) starts a single
detached **watcher daemon** on server boot. The watcher discovers the active
Codex rollouts under `CODEX_HOME` — via the Codex thread state DB
(`state_5.sqlite`), falling back to an mtime scan of
`CODEX_HOME/sessions/<YYYY>/<MM>/<DD>/rollout-*.jsonl` — and incrementally tails
each one into a paired **exhaust** DAG. It is a singleton: a second Codex session
reuses the running watcher (recorded in `<SCHOLIALANG_HOME>/codex-exhaust/watcher.json`)
rather than starting a rival. The shared `scholialang_mcp_server.py` is left
byte-identical across the claude-code/codex/ollama variants; the trigger lives
only in the Codex entrypoint wrapper and the watcher.

The exhaust DAG is tagged `exhaust` + `event-source` and titled
`"<checkpoint title> — exhaust"`, keyed to the Codex session's checkpoint trace by
the same `session_id` the skill tells Codex to use (`host: "codex"`, `session_id`
= the Codex thread id). The Scholia Live viewer's existing **Checkpoint/Exhaust**
toggle then pairs and switches between the two traces for that session — no viewer
changes required.

```text
SCHOLIA_EXHAUST=0                 # 0 / false / off / no  — disable live exhaust only (on by default)
SCHOLIA_EXHAUST_MAX_EVENTS=2000   # optional; cap on captured rollout events (default 2000)
SCHOLIA_EXHAUST_ACTIVE_WINDOW_S   # optional; only tail rollouts modified within this many seconds (default 6h)
```

Capture rides on the shared auto-emit opt-out: `SCHOLIA_AUTOEMIT=0` or a
`.scholia-off` marker in the project root disables it along with checkpoint
emission. `SCHOLIA_EXHAUST=0` disables *only* live exhaust (keeping checkpoint).
It honors `CODEX_HOME` and `SCHOLIALANG_HOME`. It is idempotent: each rollout line
maps to a stable per-line atom id (`cxline_<line>`) and the watcher resumes from
the last imported line, so a restart mid-session never duplicates atoms. The cap
is enforced via `SCHOLIA_EXHAUST_MAX_EVENTS`, there are no DB schema changes, and
**all capture failures are swallowed — live exhaust never breaks a Codex session.**

**Alternative trigger (`notify`):** Codex's `notify` hook in `~/.codex/config.toml`
can also drive capture, but it requires editing the user's global Codex config and
only fires on turn-completion notifications, so the poll-daemon above is the
default. To wire `notify` instead, point it at
`scripts/codex_exhaust_watcher.py --once` so each notification runs a single
discovery+capture pass.

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
verified to import both raw events and canonical internal agent harness events
into SQLite-backed Scholialang DAGs.
