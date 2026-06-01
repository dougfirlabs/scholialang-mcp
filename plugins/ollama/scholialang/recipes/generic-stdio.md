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

## Required Capabilities

The harness must:

1. Spawn the server as a long-lived subprocess (one server per session is
   fine; the SQLite store handles concurrent access via its own locking).
2. Communicate via JSON-RPC 2.0 over stdio (the MCP standard).
3. Respect the `protocolVersion` returned in `initialize` (`2025-11-25`
   at this writing).
4. Send `tools/list` after `initialize` to discover the
   `scholia.*` tool surface.

## Suggested Auto-Approve List

If your harness supports per-tool approval policy, mark these as
auto-approve for ergonomic use:

- `scholia.catalog`
- `scholia.lookup`
- `scholia.dag_summary`
- `scholia.dag_search`
- `scholia.dag_frontier`
- `scholia.dag_neighbors`
- `scholia.lint_snippet`
- `scholia.lint_trace`

These are all read-only or pure-function tools. Leave write tools
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
