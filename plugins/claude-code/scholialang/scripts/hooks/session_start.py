#!/usr/bin/env python3
"""Claude Code SessionStart hook: ensure this project's session trace exists.

Deterministically opens (or resumes) the per-project Scholialang session DAG for
this Claude Code session and tells Claude to append semantic atoms at meaningful
boundaries. Honors the shared opt-out (SCHOLIA_AUTOEMIT / .scholia-off) via the
server. Never fails the session: any error exits 0 with no injected context.
"""
import json
import os
import sys
from pathlib import Path

HOST = "claude-code"


def _emit_context(text):
    if text:
        print(
            json.dumps(
                {
                    "hookSpecificOutput": {
                        "hookEventName": "SessionStart",
                        "additionalContext": text,
                    }
                }
            )
        )


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

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import scholialang_mcp_server as server  # noqa: E402

    result = server.tool_dag_ensure_session(
        {"project_path": cwd, "session_id": session_id, "host": HOST, "auto": True}
    )
    structured = result.get("structuredContent", {})

    if not structured.get("enabled", False):
        # Opt-out active (SCHOLIA_AUTOEMIT / .scholia-off). Stay silent.
        return

    dag_id = structured.get("dag_id", "unknown")
    context = (
        f"Scholialang auto-emit is ON for this project. A session trace DAG is active: {dag_id} "
        f"(host=claude-code, session={session_id}).\n"
        "As you work, append concise Scholialang atoms at meaningful boundaries with "
        "scholia_dag_add_atom (use the dag_id above): Observation for notable command/file/query "
        "results, Deciding at branch points (name the chosen path), Finding for conclusions, Action "
        "for durable external changes, and Contradiction/Retract when something invalidates a prior "
        "atom. Link new atoms to prior ones via `links` (derived_from / supports / refutes / "
        "depends_on). At the end of the session append a Concluding atom referencing the Goal. Keep "
        "summaries short, reference files by path instead of pasting contents, and never record "
        "secrets. To disable: `export SCHOLIA_AUTOEMIT=0` or add a `.scholia-off` file in the "
        "project root."
    )
    _emit_context(context)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Tracing must never break the session.
        pass
    sys.exit(0)
