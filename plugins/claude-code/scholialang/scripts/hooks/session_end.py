#!/usr/bin/env python3
"""Claude Code SessionEnd hook: close this project's session trace.

Appends a Summary atom marking session end to the per-project session DAG, if
one exists (none exists when auto-emit was opted out at start, in which case this
is a safe no-op). Never fails the session.
"""
import json
import os
import sys
from pathlib import Path

HOST = "claude-code"


def main():
    try:
        raw = sys.stdin.read()
    except Exception:
        raw = ""
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        payload = {}

    cwd = payload.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    session_id = payload.get("session_id") or os.environ.get("CLAUDE_SESSION_ID") or "default"
    reason = payload.get("reason") or "session end"

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import scholialang_mcp_server as server  # noqa: E402

    server.tool_dag_finish_session(
        {
            "project_path": cwd,
            "session_id": session_id,
            "host": HOST,
            "kind": "Summary",
            "summary": f"Claude Code session ended ({reason}).",
        }
    )


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
