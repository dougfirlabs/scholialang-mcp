#!/usr/bin/env python3
"""Claude Code SessionStart hook: ensure this project's session trace exists.

Deterministically opens (or resumes) the per-project Scholialang session DAG for
this Claude Code session and tells Claude to append semantic atoms at meaningful
boundaries. Honors the shared opt-out (SCHOLIA_AUTOEMIT / .scholia-off) via the
server. Never fails the session: any error exits 0 with no injected context.
"""
import json
import os
import socket
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote

HOST = "claude-code"
LIVE_ON_VALUES = {"1", "true", "on", "yes"}
DEFAULT_LIVE_PORT = 8765


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


def _live_enabled():
    flag = os.environ.get("SCHOLIA_LIVE")
    return flag is not None and flag.strip().lower() in LIVE_ON_VALUES


def _exhaust_enabled():
    # Default ON: live exhaust capture is mechanical and free (zero added LLM
    # tokens), so it runs whenever auto-emit is on. The shared opt-out
    # (SCHOLIA_AUTOEMIT=0 / .scholia-off) is enforced by the caller; an explicit
    # SCHOLIA_EXHAUST in {0,false,off,no} force-disables just the exhaust tailer.
    flag = os.environ.get("SCHOLIA_EXHAUST")
    if not flag:
        return True
    return flag.strip().lower() not in {"0", "false", "off", "no"}


def _scholialang_home():
    return Path(os.environ.get("SCHOLIALANG_HOME") or "~/.scholialang").expanduser()


def _live_port_pref():
    raw = os.environ.get("SCHOLIA_LIVE_PORT")
    try:
        return int(raw) if raw else DEFAULT_LIVE_PORT
    except (TypeError, ValueError):
        return DEFAULT_LIVE_PORT


def _pid_alive(pid):
    try:
        os.kill(int(pid), 0)
    except (OSError, TypeError, ValueError):
        return False
    return True


def _free_port(preferred):
    for port in range(preferred, preferred + 11):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.bind(("127.0.0.1", port))
            return port
        except OSError:
            continue
        finally:
            sock.close()
    return preferred


def _maybe_launch_live(cwd):
    """Start the Scholia Live dashboard as a singleton when SCHOLIA_LIVE is on.

    Returns the viewer URL when live mode is enabled, else None. Reuses a running
    server recorded in <home>/live-server.json when its pid is still alive.
    """
    if not _live_enabled():
        return None
    home = _scholialang_home()
    state_path = home / "live-server.json"
    try:
        existing = json.loads(state_path.read_text())
    except (OSError, ValueError):
        existing = {}

    if _pid_alive(existing.get("pid")):
        port = int(existing.get("port", _live_port_pref()))
    else:
        script = Path(__file__).resolve().parent.parent / "scholialang_webview_server.py"
        if not script.exists():
            return None
        port = _free_port(_live_port_pref())
        home.mkdir(parents=True, exist_ok=True)
        log = open(home / "live-server.log", "ab")  # noqa: SIM115 (inherited by child)
        proc = subprocess.Popen(
            [
                sys.executable, str(script),
                "--host", "127.0.0.1", "--port", str(port),
                "--project-path", cwd, "--quiet",
            ],
            stdout=log, stderr=log, stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        try:
            state_path.write_text(json.dumps({"pid": proc.pid, "port": port}))
        except OSError:
            pass

    return "http://127.0.0.1:{}/?project_path={}".format(port, quote(cwd))


def _exhaust_state_path(session_id):
    return _scholialang_home() / "exhaust" / f"{session_id}.json"


def _maybe_launch_exhaust(cwd, session_id, transcript_path):
    """Start the per-session exhaust tailer as a singleton (default on; SCHOLIA_EXHAUST=0 disables).

    Mechanical, out-of-band capture: the tailer parses the transcript Claude Code
    already writes and appends exhaust atoms to a paired exhaust DAG. Reuses a
    running tailer recorded in <home>/exhaust/<session>.json when its pid is still
    alive. Returns the resolved transcript path when launched, else None. Never
    raises into the caller.
    """
    if not _exhaust_enabled():
        return None
    home = _scholialang_home()
    state_path = _exhaust_state_path(session_id)
    try:
        existing = json.loads(state_path.read_text())
    except (OSError, ValueError):
        existing = {}
    if _pid_alive(existing.get("pid")):
        return existing.get("transcript")

    script = Path(__file__).resolve().parent / "exhaust_tailer.py"
    if not script.exists():
        return None
    if not transcript_path:
        try:
            import cc_exhaust  # scripts dir already on sys.path
            transcript_path = str(cc_exhaust.transcript_path_for(cwd, session_id))
        except Exception:
            return None

    home.mkdir(parents=True, exist_ok=True)
    log = open(home / "exhaust.log", "ab")  # noqa: SIM115 (inherited by child)
    subprocess.Popen(
        [
            sys.executable, str(script),
            "--transcript", str(transcript_path),
            "--project-path", cwd,
            "--session-id", session_id,
        ],
        stdout=log, stderr=log, stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    return str(transcript_path)


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
    transcript_path = payload.get("transcript_path")

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import scholialang_mcp_server as server  # noqa: E402

    result = server.tool_dag_ensure_session(
        {"project_path": cwd, "session_id": session_id, "host": HOST, "auto": True}
    )
    structured = result.get("structuredContent", {})

    live_url = None
    try:
        live_url = _maybe_launch_live(cwd)
    except Exception:
        live_url = None

    # Live exhaust capture (default ON, free/mechanical): runs whenever auto-emit
    # is on, so the shared opt-out suppresses it; SCHOLIA_EXHAUST=0 force-disables
    # just exhaust. Never breaks start.
    if structured.get("enabled", False):
        try:
            _maybe_launch_exhaust(cwd, session_id, transcript_path)
        except Exception:
            pass

    if not structured.get("enabled", False):
        # Auto-emit opted out: stay silent about emission, but still surface the
        # live viewer if SCHOLIA_LIVE enabled it.
        if live_url:
            _emit_context(f"Scholia Live viewer is running: {live_url}")
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
    if live_url:
        context = f"{context}\nScholia Live viewer: {live_url}"
    _emit_context(context)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        # Tracing must never break the session.
        pass
    sys.exit(0)
