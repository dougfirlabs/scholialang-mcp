# Generic Stdio MCP Host

If your Ollama-backed harness speaks MCP over stdio but isn't listed in the
other recipes, this is the integration shape. The MCP server is a single
Python script; the harness spawns it as a subprocess, writes JSON-RPC
messages to its stdin, and reads JSON-RPC responses from its stdout.

## Spawn Command

```
python3 <absolute-path-to>/plugins/ollama/scholialang/scripts/scholialang_mcp_server.py
```

Working directory must be a directory the harness can read from; the
server itself doesn't write to its `cwd`. Set the `SCHOLIALANG_HOME`
environment variable if you want to override the default
`~/.scholialang` storage root.

Set `SCHOLIA_HOST` to a stable harness name and, when the harness exposes one,
set `SCHOLIA_SESSION_ID` to the conversation identifier. Explicit `host` and
`session_id` tool arguments take precedence. Without a host-provided session
identifier, the server creates a random process-scoped identity; it remains
idempotent for that server process and never falls back to a cross-host
`unknown:default` DAG.

## Required Capabilities

The harness must:

1. Spawn the server as a long-lived subprocess (one server per session is
   fine; the SQLite store handles concurrent access via its own locking).
2. Communicate via JSON-RPC 2.0 over stdio (the MCP standard).
3. Respect the `protocolVersion` returned in `initialize` (`2025-11-25`
   at this writing).
4. Send `tools/list` after `initialize` to discover the
   `scholia.*` tool surface.

## Auto-Emit (Default)

Scholialang auto-emits a per-project trace by default. Generic MCP hosts have no
lifecycle hooks, so this is model-driven: paste `recipes/autoemit-system-prompt.md`
into your harness's system prompt. The model then calls `scholia_dag_ensure_session`
(idempotent) at the start of work and appends atoms at meaningful boundaries.

Opt out with `SCHOLIA_AUTOEMIT=0` in the server's environment, or a `.scholia-off`
file in the project root — both are enforced server-side.

## Suggested Auto-Approve List

If your harness supports per-tool approval policy, mark these as
auto-approve for ergonomic use:

- `scholia_catalog`
- `scholia_lookup`
- `scholia_dag_summary`
- `scholia_dag_search`
- `scholia_dag_frontier`
- `scholia_dag_neighbors`
- `scholia_lint_snippet`
- `scholia_lint_trace`
- `scholia_dag_ensure_session`
- `scholia_dag_finish_session`

The first eight are read-only or pure-function tools. The two
`*_session` tools are idempotent session-lifecycle helpers — auto-approve
them so default auto-emit can open and close the per-project session DAG
without a prompt every session. Leave the content write tools
(`dag_start`, `dag_add_atom`, `dag_link`, `dag_compact`,
`codex_import_thread`) on the standard prompt-for-approval path so the
user keeps editorial control over what lands in the persistent DAG.

## Sanity Check

Spawn the server manually and pipe a `tools/list` request through stdin
to confirm the integration shape:

```sh
python3 -c '
import json, subprocess
p = subprocess.Popen(
    ["python3", "plugins/ollama/scholialang/scripts/scholialang_mcp_server.py"],
    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
)
def send(msg):
    body = json.dumps(msg).encode()
    p.stdin.write(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
    p.stdin.flush()
send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
# Read responses; abbreviated for brevity.
'
```

A successful run lists ~16 tools under the `scholia.*` namespace.
