#!/usr/bin/env python3
"""Claude Code SessionEnd hook: close this project's session trace.

Appends a canonical Concluding atom to the per-project session DAG, if
one exists (none exists when auto-emit was opted out at start, in which case this
is a safe no-op). Never fails the session.
"""
import json
import os
import signal
import sys
from pathlib import Path

HOST = "claude-code"


def _scholialang_home():
    return Path(os.environ.get("SCHOLIALANG_HOME") or "~/.scholialang").expanduser()


def _stop_exhaust(home, session_id):
    """Terminate this session's exhaust tailer (if any) and drop its state file.

    Safe no-op when no exhaust capture ran (state file absent). Never raises.
    """
    path = Path(home) / "exhaust" / f"{session_id}.json"
    try:
        state = json.loads(path.read_text())
    except (OSError, ValueError):
        state = None
    if state:
        pid = state.get("pid")
        try:
            if pid and int(pid) != os.getpid():
                os.kill(int(pid), signal.SIGTERM)
        except (OSError, TypeError, ValueError):
            pass
    try:
        path.unlink()
    except OSError:
        pass


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

    # Stop the live exhaust tailer for this session (no-op if it never ran).
    try:
        _stop_exhaust(_scholialang_home(), session_id)
    except Exception:
        pass

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import scholialang_mcp_server as server  # noqa: E402

    server.tool_dag_finish_session(
        {
            "project_path": cwd,
            "session_id": session_id,
            "host": HOST,
            "summary": f"Claude Code session ended ({reason}).",
        }
    )


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
