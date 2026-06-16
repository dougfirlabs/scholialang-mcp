#!/usr/bin/env python3
"""Background exhaust tailer for Claude Code.

Follows the active session's transcript JSONL and appends new exhaust atoms to
the session's exhaust DAG as lines arrive. Launched detached by the SessionStart
hook only when SCHOLIA_EXHAUST is enabled; stopped by SessionEnd.

Design constraints (PRD ext-scholialang-live-exhaust):
  * Mechanical only — no model/network calls (the capture path lives in
    ``cc_exhaust`` and touches only the transcript bytes + local SQLite).
  * Never breaks the session: every error path is swallowed and the process
    exits 0.
  * Idempotent: resumes from the last imported line recorded in a per-session
    state file, and the stable per-line atom ids defend against duplicates even
    if that state file is lost.
"""
import argparse
import json
import os
import signal
import sys
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import cc_exhaust as cc  # noqa: E402
import scholialang_mcp_server as server  # noqa: E402

# The tailer wakes every POLL_SECONDS and writes all transcript lines that
# arrived since the last pass as one batch, bounding SQLite churn while still
# feeling live in the viewer.
POLL_SECONDS = 1.0
IDLE_EXIT_SECONDS = 6 * 60 * 60  # stop an orphaned tailer after long inactivity


def state_path(home, session_id):
    return Path(home) / "exhaust" / f"{session_id}.json"


def load_state(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}


def save_state(path, state):
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_text(json.dumps(state))
    except OSError:
        pass


def sync_state(state, *, transcript_path, project_path, max_events, log=None):
    """Run one capture pass and fold the result into ``state`` (mutated + returned)."""
    start_line = int(state.get("last_line", 0)) + 1
    result = cc.capture_once(
        server,
        transcript_path=transcript_path,
        dag_id=state["dag_id"],
        project_path=project_path,
        max_events=max_events,
        start_line=start_line,
        previous_atom_id=state.get("last_atom_id"),
        log=log,
    )
    state["last_line"] = max(int(state.get("last_line", 0)), result.last_line)
    if result.last_atom_id:
        state["last_atom_id"] = result.last_atom_id
    state["appended_total"] = int(state.get("appended_total", 0)) + result.appended
    state["truncated"] = result.truncated
    return state, result


def run(*, transcript_path, project_path, session_id, max_events, poll=POLL_SECONDS, log=None):
    """Resolve the exhaust DAG, then tail the transcript until killed/idle/capped."""
    log = log or (lambda _msg: None)
    home = cc.scholialang_home()
    path = state_path(home, session_id)
    state = load_state(path)

    info = cc.ensure_exhaust_dag(server, project_path=project_path, session_id=session_id)
    if not info or not info.get("dag_id"):
        log("exhaust capture opted out; nothing to tail")
        return 0
    state["dag_id"] = info["dag_id"]
    state["pid"] = os.getpid()
    state["transcript"] = str(transcript_path)
    save_state(path, state)
    log(f"tailing {transcript_path} -> exhaust DAG {info['dag_id']}")

    last_progress = time.monotonic()
    while True:
        try:
            state, result = sync_state(
                state, transcript_path=transcript_path, project_path=project_path,
                max_events=max_events, log=log,
            )
            save_state(path, state)
            if result.appended:
                last_progress = time.monotonic()
            if result.truncated:
                # The cap is reached: no later line can become an atom, so the
                # tailer's work is done.
                log("max_events cap reached; exhaust tailer stopping")
                break
        except Exception as exc:  # tracing must never break the session
            log(f"exhaust sync error (continuing): {exc}")
        if time.monotonic() - last_progress > IDLE_EXIT_SECONDS:
            log("exhaust tailer idle timeout; stopping")
            break
        time.sleep(poll)
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Scholialang Claude Code exhaust tailer")
    parser.add_argument("--transcript", required=True)
    parser.add_argument("--project-path", required=True)
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--max-events", type=int, default=None)
    parser.add_argument("--poll", type=float, default=POLL_SECONDS)
    args = parser.parse_args(argv)

    max_events = args.max_events if args.max_events else cc.max_events_pref()

    # Exit cleanly on SessionEnd's SIGTERM.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    run(
        transcript_path=args.transcript,
        project_path=args.project_path,
        session_id=args.session_id,
        max_events=max_events,
        poll=args.poll,
        log=lambda msg: print(msg, file=sys.stderr, flush=True),
    )


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        # Tracing must never break the session.
        pass
    sys.exit(0)
