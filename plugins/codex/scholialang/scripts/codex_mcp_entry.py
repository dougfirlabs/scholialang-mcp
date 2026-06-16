#!/usr/bin/env python3
"""Codex MCP entrypoint: launch the live exhaust watcher, then serve.

Codex has no SessionStart/SessionEnd plugin hooks, so there is no per-session
place to start live exhaust capture the way the Claude Code plugin does. The one
no-config signal that "a Codex session is live" is the MCP server boot itself.

The shared ``scholialang_mcp_server.py`` is kept byte-identical across the
claude-code / codex / ollama plugin variants (enforced by a parity test), so the
Codex-specific trigger must NOT live inside it. Instead, the Codex plugin's
``.mcp.json`` launches this thin wrapper: it spawns the detached singleton
exhaust watcher (mechanical, zero added LLM tokens) and then hands control to the
unmodified shared server's stdio loop. Best-effort and isolated — if the watcher
launch fails, the MCP server still starts normally and the Codex session is
unaffected."""
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import scholialang_mcp_server as server  # noqa: E402


def _trigger_exhaust_watcher():
    try:
        import codex_exhaust_watcher

        codex_exhaust_watcher.maybe_launch()
    except Exception:
        # Tracing must never break a Codex session.
        pass


if __name__ == "__main__":
    _trigger_exhaust_watcher()
    server.main()
