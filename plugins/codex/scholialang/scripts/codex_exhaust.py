#!/usr/bin/env python3
"""Codex rollout -> Scholialang exhaust atoms (mechanical, zero-token).

Incremental, idempotent tailer for the Codex rollout JSONL that Codex already
writes under ``CODEX_HOME/sessions/<YYYY>/<MM>/<DD>/rollout-<ts>-<uuid>.jsonl``.
It is the live sibling of ``scholia_codex_import_thread``: where that tool does a
one-shot full import (raw + canonical envelopes), this module appends only NEW
rollout events to a per-session **exhaust** DAG as lines arrive.

The event -> atom conversion is **not** reimplemented here. Each non-blank
rollout line is handed to the existing parser in ``scholialang_mcp_server``
(``codex_event_atom_kind`` / ``codex_event_summary`` / ``codex_event_content``),
exactly the per-line conversion ``tool_codex_import_thread`` uses. The only thing
this module adds is a stable per-line atom id (``cxline_<line>``) so re-parsing /
resuming a growing rollout never duplicates atoms (idempotent by line number).

No network or model calls happen here: the only inputs are the rollout bytes and
the (local SQLite) DAG write path passed in as ``server``.
"""
import json
import os
import re
import sqlite3
from pathlib import Path

DEFAULT_MAX_EVENTS = 2000
ATOM_ID_PREFIX = "cxline"
EXHAUST_HOST = "codex-exhaust"
CHECKPOINT_HOST = "codex"
EXHAUST_TITLE_SUFFIX = " — exhaust"

# A v7-style session/thread uuid as embedded in a rollout filename, e.g.
# rollout-2026-06-15T11-44-51-019ecc99-cc36-7eb0-81ed-17f8e99eb6ee.jsonl
_UUID_RE = re.compile(
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)


# --------------------------------------------------------------------------- #
# Config / paths
# --------------------------------------------------------------------------- #
def max_events_pref(env=None):
    env = os.environ if env is None else env
    raw = env.get("SCHOLIA_EXHAUST_MAX_EVENTS")
    try:
        value = int(raw) if raw else DEFAULT_MAX_EVENTS
    except (TypeError, ValueError):
        return DEFAULT_MAX_EVENTS
    return value if value > 0 else DEFAULT_MAX_EVENTS


def scholialang_home(env=None):
    env = os.environ if env is None else env
    return Path(env.get("SCHOLIALANG_HOME") or "~/.scholialang").expanduser()


def codex_home(env=None):
    env = os.environ if env is None else env
    return Path(env.get("CODEX_HOME") or "~/.codex").expanduser()


def exhaust_enabled(env=None):
    """Default ON: live exhaust capture is mechanical and free (zero added LLM
    tokens). The shared opt-out (``SCHOLIA_AUTOEMIT=0`` / ``.scholia-off``) is the
    primary gate; an explicit ``SCHOLIA_EXHAUST`` in {0,false,off,no} force-disables
    just exhaust. Returns False when either off-switch is set."""
    env = os.environ if env is None else env
    autoemit = env.get("SCHOLIA_AUTOEMIT")
    if autoemit is not None and autoemit.strip().lower() in {"0", "false", "off", "no"}:
        return False
    flag = env.get("SCHOLIA_EXHAUST")
    if not flag:
        return True
    return flag.strip().lower() not in {"0", "false", "off", "no"}


# --------------------------------------------------------------------------- #
# Rollout discovery (which rollouts are active, and where do they live)
# --------------------------------------------------------------------------- #
def session_id_from_rollout_path(path):
    """Extract the Codex session/thread uuid embedded in a rollout filename.

    Falls back to the filename stem when no uuid is present (defensive)."""
    match = _UUID_RE.search(Path(path).name)
    if match:
        return match.group(1)
    return Path(path).stem


def rollout_session_meta(path):
    """Parse the leading ``session_meta`` line for (session_id, cwd).

    The first non-blank rollout line is a ``session_meta`` record whose payload
    carries the thread ``id`` and the working directory ``cwd``. Returns a dict
    with whatever could be resolved; never raises."""
    meta = {"session_id": session_id_from_rollout_path(path), "cwd": None}
    try:
        with open(path, "r", errors="replace") as handle:
            for raw in handle:
                raw = raw.strip()
                if not raw:
                    continue
                obj = json.loads(raw)
                payload = obj.get("payload") if isinstance(obj, dict) else None
                if isinstance(payload, dict):
                    if payload.get("id"):
                        meta["session_id"] = payload.get("id")
                    if payload.get("cwd"):
                        meta["cwd"] = payload.get("cwd")
                break
    except (OSError, ValueError):
        pass
    return meta


def discover_active_rollouts(home=None, *, window_seconds=None, limit=64, now=None):
    """Return active Codex rollouts as ``{session_id, rollout_path, project_path, title}``.

    Primary source is the Codex thread state DB (``CODEX_HOME/state_5.sqlite``):
    non-archived threads, most-recently-updated first, give the rollout path, the
    working directory (cwd), the thread id and title without parsing every file.
    Falls back to an mtime scan of ``sessions/**/*.jsonl`` (reading each file's
    ``session_meta`` for cwd) when the state DB is absent. ``window_seconds``, when
    set, drops rollouts whose backing file has not changed within the window so a
    machine-wide watcher only tails *active* sessions. Never raises."""
    home = Path(home) if home is not None else codex_home()
    try:
        rows = _discover_from_state_db(home, limit=limit)
    except Exception:
        rows = []
    if not rows:
        try:
            rows = _discover_from_filesystem(home, limit=limit)
        except Exception:
            rows = []

    if window_seconds is None:
        return rows
    now = now if now is not None else _now_seconds()
    fresh = []
    for row in rows:
        try:
            mtime = Path(row["rollout_path"]).stat().st_mtime
        except OSError:
            continue
        if now - mtime <= window_seconds:
            fresh.append(row)
    return fresh


def _now_seconds():
    # Wrapped so callers/tests can inject a clock without importing time here.
    import time

    return time.time()


def _discover_from_state_db(home, *, limit):
    state_path = Path(home) / "state_5.sqlite"
    if not state_path.exists():
        return []
    conn = sqlite3.connect(str(state_path))
    conn.row_factory = sqlite3.Row
    try:
        cursor = conn.execute(
            """
            SELECT id, rollout_path, cwd, title
            FROM threads
            WHERE archived = 0 AND rollout_path IS NOT NULL
            ORDER BY updated_at_ms DESC, updated_at DESC
            LIMIT ?
            """,
            (int(limit),),
        )
        rows = []
        for row in cursor.fetchall():
            rollout_path = row["rollout_path"]
            if not rollout_path or not Path(rollout_path).expanduser().exists():
                continue
            rows.append(
                {
                    "session_id": row["id"] or session_id_from_rollout_path(rollout_path),
                    "rollout_path": str(Path(rollout_path).expanduser()),
                    "project_path": row["cwd"],
                    "title": row["title"],
                }
            )
        return rows
    finally:
        conn.close()


def _discover_from_filesystem(home, *, limit):
    sessions = Path(home) / "sessions"
    if not sessions.exists():
        return []
    files = [p for p in sessions.rglob("rollout-*.jsonl") if p.is_file()]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    rows = []
    for path in files[: int(limit)]:
        meta = rollout_session_meta(path)
        rows.append(
            {
                "session_id": meta["session_id"],
                "rollout_path": str(path),
                "project_path": meta["cwd"],
                "title": None,
            }
        )
    return rows


# --------------------------------------------------------------------------- #
# Pure parser (reuses the server's event -> atom conversion)
# --------------------------------------------------------------------------- #
def atom_id_for(line_no):
    return f"{ATOM_ID_PREFIX}_{int(line_no):05d}"


def _line_to_atom(server, line_no, raw_line, source=None):
    """Map one rollout line to one exhaust atom, reusing the server parser.

    Mirrors the per-line branch of ``tool_codex_import_thread``: malformed JSON ->
    Contradiction, non-object JSON -> Observation, otherwise the kind/summary/
    content come straight from ``codex_event_atom_kind`` / ``codex_event_summary``
    / ``codex_event_content``. The only addition is the stable ``cxline_<line>``
    atom id."""
    files = [f"{source}:{line_no}"] if source else []
    try:
        obj = json.loads(raw_line)
    except Exception as exc:  # malformed JSON -> Contradiction, like the importer
        content = json.dumps(
            {"line": line_no, "raw_line_sha256": server.sha256_text(raw_line), "error": str(exc)},
            indent=2,
            sort_keys=True,
        )
        return {
            "atom_id": atom_id_for(line_no),
            "line": line_no,
            "kind": "Contradiction",
            "summary": f"Codex rollout event {line_no:04d}: JSON parse error",
            "content": content,
            "files": files,
        }

    if not isinstance(obj, dict):
        content = json.dumps(
            {
                "event_index": line_no,
                "line": line_no,
                "payload_type": f"json_{type(obj).__name__}",
                "raw_line_sha256": server.sha256_text(raw_line),
                "value": server.json_text_record("value", obj, False, 1200),
            },
            indent=2,
            sort_keys=True,
        )
        return {
            "atom_id": atom_id_for(line_no),
            "line": line_no,
            "kind": "Observation",
            "summary": f"Codex rollout event {line_no:04d}: non-object JSON value",
            "content": content,
            "files": files,
        }

    payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
    payload_type = payload.get("type", obj.get("type", "unknown"))
    top_type = obj.get("type", "unknown")
    return {
        "atom_id": atom_id_for(line_no),
        "line": line_no,
        "kind": server.codex_event_atom_kind(payload_type, payload),
        "summary": server.codex_event_summary(line_no, top_type, payload_type, payload),
        "content": server.codex_event_content(line_no, line_no, raw_line, obj, {}),
        "files": files,
    }


class ParseResult:
    __slots__ = ("atoms", "truncated", "scanned")

    def __init__(self, atoms, truncated, scanned):
        self.atoms = atoms
        self.truncated = truncated
        self.scanned = scanned


def parse_rollout_lines(server, lines, *, max_events=DEFAULT_MAX_EVENTS, start_line=1, source=None):
    """Map rollout lines to exhaust atoms.

    Deterministic given the same bytes. ``max_events`` caps the absolute line
    number that may become an atom (matching ``tool_codex_import_thread``'s
    ``raw_lines[:max_events]``), so the exhaust DAG never exceeds the cap
    regardless of how the tailer batches. Lines before ``start_line`` are skipped
    (resume). Blank lines are skipped but keep their line numbers, so atom ids
    remain a stable function of line number."""
    if max_events <= 0:
        max_events = DEFAULT_MAX_EVENTS
    total = len(lines)
    limit = min(total, max_events)
    truncated = total > max_events
    atoms = []
    for line_no in range(max(1, start_line), limit + 1):
        raw = lines[line_no - 1]
        if not raw.strip():
            continue
        atoms.append(_line_to_atom(server, line_no, raw, source=source))
    return ParseResult(atoms=atoms, truncated=truncated, scanned=limit)


# --------------------------------------------------------------------------- #
# Capture (append to a local SQLite exhaust DAG via the server write path)
# --------------------------------------------------------------------------- #
class CaptureResult:
    __slots__ = ("appended", "scanned", "truncated", "last_atom_id", "last_line")

    def __init__(self, appended, scanned, truncated, last_atom_id, last_line):
        self.appended = appended
        self.scanned = scanned
        self.truncated = truncated
        self.last_atom_id = last_atom_id
        self.last_line = last_line


def append_atoms(server, dag_id, project_path, atoms, previous_atom_id=None):
    """Append parsed atoms to the exhaust DAG, skipping any already present.

    Stable per-line atom ids make this idempotent: re-adding an existing id is a
    no-op (the server raises ``atom already exists``, which we swallow)."""
    appended = 0
    last = previous_atom_id
    for atom in atoms:
        links = [{"to": last, "relation": "after"}] if last else []
        try:
            result = server.tool_dag_add_atom(
                {
                    "dag_id": dag_id,
                    "project_path": project_path,
                    "atom_id": atom["atom_id"],
                    "kind": atom["kind"],
                    "summary": atom["summary"],
                    "content": atom["content"],
                    "files": atom.get("files", []),
                    "links": links,
                }
            )
        except ValueError as exc:
            if "already exists" in str(exc):
                last = atom["atom_id"]
                continue
            raise
        last = result["structuredContent"]["atom"]["id"]
        appended += 1
    return appended, last


def capture_once(
    server,
    *,
    rollout_path,
    dag_id,
    project_path,
    max_events=DEFAULT_MAX_EVENTS,
    start_line=1,
    previous_atom_id=None,
    log=None,
):
    """Read the rollout once, parse from ``start_line``, append new atoms."""
    path = Path(rollout_path)
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return CaptureResult(0, start_line - 1, False, previous_atom_id, start_line - 1)
    parsed = parse_rollout_lines(server, lines, max_events=max_events, start_line=start_line, source=str(path))
    if parsed.truncated and callable(log):
        log(f"exhaust capture stopped at max_events={max_events}; {len(lines) - max_events} later events skipped")
    appended, last = append_atoms(server, dag_id, project_path, parsed.atoms, previous_atom_id)
    return CaptureResult(
        appended=appended,
        scanned=parsed.scanned,
        truncated=parsed.truncated,
        last_atom_id=last,
        last_line=parsed.scanned,
    )


# --------------------------------------------------------------------------- #
# Pairing: create the exhaust DAG tagged + titled to match the checkpoint DAG
# --------------------------------------------------------------------------- #
def ensure_exhaust_dag(server, *, project_path, session_id, host=CHECKPOINT_HOST, exhaust_host=EXHAUST_HOST):
    """Find-or-create this session's Codex exhaust DAG, paired to the checkpoint DAG.

    Resolves the ``host=codex`` checkpoint session DAG (honoring the shared
    opt-out) to read its title — the SKILL guidance has Codex open that DAG with
    ``session_id`` = the Codex thread id, which is exactly the id we derive from
    the rollout — then ensures a sibling exhaust DAG titled
    ``"<checkpoint> — exhaust"`` and tagged ``exhaust`` + ``event-source`` so
    ``trace_view_mode`` resolves to ``exhaust`` and ``related_trace_views`` pairs
    the two (``trace_match_score >= 42``). Returns ``None`` when auto-emit is
    opted out (no exhaust capture)."""
    checkpoint = server.tool_dag_ensure_session(
        {"project_path": project_path, "session_id": session_id, "host": host, "auto": True}
    ).get("structuredContent", {})
    if not checkpoint.get("enabled", False):
        return None
    checkpoint_title = checkpoint.get("title") or ""
    exhaust_title = f"{checkpoint_title}{EXHAUST_TITLE_SUFFIX}"
    exhaust = server.tool_dag_ensure_session(
        {
            "project_path": project_path,
            "session_id": session_id,
            "host": exhaust_host,
            "auto": True,
            "tags": ["exhaust", "event-source"],
            "title": exhaust_title,
            "objective": f"Live Codex rollout exhaust trail paired to the {host} checkpoint session.",
        }
    ).get("structuredContent", {})
    return {
        "dag_id": exhaust.get("dag_id"),
        "title": exhaust_title,
        "checkpoint_dag_id": checkpoint.get("dag_id"),
        "checkpoint_title": checkpoint_title,
    }
