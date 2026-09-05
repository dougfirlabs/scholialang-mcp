#!/usr/bin/env python3
"""Background exhaust watcher for Codex.

Codex has no SessionStart/SessionEnd plugin hooks (unlike Claude Code), so the
per-session "launch a tailer" trick the Claude Code plugin uses cannot be
reused. Instead, this is a **single machine-wide watcher daemon**: it discovers
the active Codex rollouts under ``CODEX_HOME`` (via the thread state DB, falling
back to an mtime scan) and tails each one into its paired exhaust DAG through the
incremental importer in ``codex_exhaust``.

Chosen trigger (discovery-spike outcome) — **poll-daemon, launched from the MCP
server boot via the Codex entrypoint wrapper**, NOT Codex ``notify``:
  * Codex ``notify`` (``config.toml``) would require editing the user's
    ``~/.codex/config.toml`` (invasive, global, easy to clobber) and only fires
    on turn-completion notifications — it is documented as an *alternative*, not
    the default.
  * The MCP server boots once per Codex session that loads the plugin, which is
    the least-invasive no-config signal that "a Codex session is live". The
    shared ``scholialang_mcp_server.py`` is byte-identical across plugin variants
    (parity-enforced), so the Codex ``.mcp.json`` launches ``codex_mcp_entry.py``
    instead; that wrapper calls :func:`maybe_launch` (spawning this watcher as a
    detached singleton) and then hands off to the unmodified shared server. The
    watcher discovers and tails every active rollout, so one daemon serves all
    concurrent Codex sessions.

Design constraints (PRD ext-scholialang-codex-live-exhaust):
  * Mechanical only — no model/network calls (capture lives in ``codex_exhaust``
    and touches only rollout bytes + local SQLite).
  * Never breaks a Codex session: every error path is swallowed; launch is
    detached and best-effort; the process exits 0.
  * Singleton: a per-machine state file under ``SCHOLIALANG_HOME`` records the
    running pid; a second trigger reuses it instead of starting a rival watcher.
  * Idempotent: per-rollout offset state + the stable ``cxline_<line>`` atom ids
    mean a restart mid-session never duplicates atoms.
"""
import argparse
import json
import os
import re
import signal
import sys
import time
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import codex_exhaust as cx  # noqa: E402
import scholialang_mcp_server as server  # noqa: E402

POLL_SECONDS = 2.0
IDLE_EXIT_SECONDS = 6 * 60 * 60  # stop an orphaned watcher after long inactivity
DEFAULT_ACTIVE_WINDOW_SECONDS = 6 * 60 * 60  # only tail rollouts touched this recently
_UNSAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")


def active_window_seconds(env=None):
    env = os.environ if env is None else env
    raw = env.get("SCHOLIA_EXHAUST_ACTIVE_WINDOW_S")
    try:
        value = int(raw) if raw else DEFAULT_ACTIVE_WINDOW_SECONDS
    except (TypeError, ValueError):
        return DEFAULT_ACTIVE_WINDOW_SECONDS
    return value if value > 0 else DEFAULT_ACTIVE_WINDOW_SECONDS


def exhaust_dir(home):
    return Path(home) / "codex-exhaust"


def watcher_state_path(home):
    return exhaust_dir(home) / "watcher.json"


def rollout_state_path(home, session_id):
    safe = _UNSAFE_NAME_RE.sub("-", str(session_id or "default"))
    return exhaust_dir(home) / f"{safe}.json"


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


def pid_alive(pid):
    try:
        os.kill(int(pid), 0)
    except (OSError, TypeError, ValueError):
        return False
    return True


# --------------------------------------------------------------------------- #
# Launch trigger (called from the MCP server boot; singleton)
# --------------------------------------------------------------------------- #
def maybe_launch(env=None, *, home=None):
    """Spawn the watcher as a detached singleton if enabled and none is running.

    Default ON; honors ``SCHOLIA_AUTOEMIT=0`` (global) and ``SCHOLIA_EXHAUST=0``.
    Per-project ``.scholia-off`` is enforced per-rollout downstream by
    ``ensure_exhaust_dag``. Reuses a live watcher recorded in
    ``<home>/codex-exhaust/watcher.json``. Returns the watcher state dict when a
    watcher is running/launched, else ``None``. Never raises into the caller."""
    env = os.environ if env is None else env
    try:
        if not cx.exhaust_enabled(env):
            return None
        home = Path(home) if home is not None else cx.scholialang_home(env)
        state_path = watcher_state_path(home)
        existing = load_state(state_path)
        if pid_alive(existing.get("pid")):
            return existing

        import subprocess

        script = Path(__file__).resolve()
        exhaust_dir(home).mkdir(parents=True, exist_ok=True)
        log = open(exhaust_dir(home) / "watcher.log", "ab")  # noqa: SIM115 (inherited by child)
        proc = subprocess.Popen(
            [sys.executable, str(script), "--daemon"],
            stdout=log,
            stderr=log,
            stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        state = {"pid": proc.pid, "started_at": server.now()}
        save_state(state_path, state)
        return state
    except Exception:
        # Tracing must never break the Codex session.
        return None


# --------------------------------------------------------------------------- #
# Capture loop
# --------------------------------------------------------------------------- #
def sync_rollout(home, rollout, *, max_events, log=None):
    """Run one capture pass for a single discovered rollout; returns appended count.

    Resolves (once) the paired exhaust DAG, caches it + the import offset in a
    per-session state file, and appends only events past the last imported line.
    Skips rollouts whose project opted out (``ensure_exhaust_dag`` returns None).
    """
    log = log or (lambda _msg: None)
    session_id = rollout.get("session_id")
    project_path = rollout.get("project_path")
    rollout_path = rollout.get("rollout_path")
    if not project_path or not rollout_path:
        return 0

    # Opt-out is a live control, not just a creation-time gate. Do this before
    # loading or mutating cached state so the cursor remains parked while
    # disabled and capture can resume without loss if the marker is removed.
    reason = server.autoemit_disabled_reason(project_path)
    if reason is not None:
        log(f"exhaust opted out for {project_path} ({reason}); skipping {rollout_path}")
        return 0

    path = rollout_state_path(home, session_id)
    state = load_state(path)
    if not state.get("dag_id"):
        info = cx.ensure_exhaust_dag(server, project_path=project_path, session_id=session_id)
        if not info or not info.get("dag_id"):
            log(f"exhaust opted out for {project_path}; skipping {rollout_path}")
            return 0
        state["dag_id"] = info["dag_id"]
        state["rollout_path"] = rollout_path
        save_state(path, state)

    start_line = int(state.get("last_line", 0)) + 1
    result = cx.capture_once(
        server,
        rollout_path=rollout_path,
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
    save_state(path, state)
    if result.appended:
        log(f"appended {result.appended} exhaust atoms from {rollout_path}")
    return result.appended


def run(*, home=None, poll=POLL_SECONDS, window_seconds=None, max_events=None, once=False, log=None):
    """Acquire the singleton, then discover + tail active rollouts until idle/killed."""
    log = log or (lambda _msg: None)
    home = Path(home) if home is not None else cx.scholialang_home()
    max_events = max_events if max_events else cx.max_events_pref()
    window_seconds = window_seconds if window_seconds is not None else active_window_seconds()

    state_path = watcher_state_path(home)
    existing = load_state(state_path)
    if not once and pid_alive(existing.get("pid")) and int(existing.get("pid")) != os.getpid():
        log(f"another watcher (pid={existing.get('pid')}) is running; exiting")
        return 0
    save_state(state_path, {"pid": os.getpid(), "started_at": server.now()})
    log(f"codex exhaust watcher started (pid={os.getpid()}, window={window_seconds}s)")

    last_progress = time.monotonic()
    while True:
        try:
            rollouts = cx.discover_active_rollouts(cx.codex_home(), window_seconds=window_seconds)
            for rollout in rollouts:
                try:
                    if sync_rollout(home, rollout, max_events=max_events, log=log):
                        last_progress = time.monotonic()
                except Exception as exc:  # one bad rollout must not stop the watcher
                    log(f"rollout sync error (continuing): {exc}")
        except Exception as exc:  # tracing must never break a session
            log(f"discovery error (continuing): {exc}")
        if once:
            break
        if time.monotonic() - last_progress > IDLE_EXIT_SECONDS:
            log("codex exhaust watcher idle timeout; stopping")
            break
        time.sleep(poll)

    # Drop the singleton marker if we still own it.
    current = load_state(state_path)
    if str(current.get("pid")) == str(os.getpid()):
        try:
            state_path.unlink()
        except OSError:
            pass
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description="Scholialang Codex exhaust watcher")
    parser.add_argument("--daemon", action="store_true", help="run the polling loop (default)")
    parser.add_argument("--once", action="store_true", help="one discovery+capture pass, then exit")
    parser.add_argument("--poll", type=float, default=POLL_SECONDS)
    parser.add_argument("--window", type=int, default=None, help="active-rollout window in seconds")
    parser.add_argument("--max-events", type=int, default=None)
    parser.add_argument("--home", default=None, help="override SCHOLIALANG_HOME")
    args = parser.parse_args(argv)

    # Exit cleanly on SIGTERM.
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    run(
        home=args.home,
        poll=args.poll,
        window_seconds=args.window,
        max_events=args.max_events,
        once=args.once,
        log=lambda msg: print(msg, file=sys.stderr, flush=True),
    )


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        # Tracing must never break a session.
        pass
    sys.exit(0)
