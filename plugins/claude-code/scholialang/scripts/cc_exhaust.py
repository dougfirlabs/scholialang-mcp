#!/usr/bin/env python3
"""Claude Code transcript -> Scholialang exhaust atoms (mechanical, zero-token).

Pure, idempotent parser plus a thin capture helper that appends to a per-session
exhaust DAG. Mirrors the mechanical shape of ``tool_codex_import_thread`` but for
the Claude Code transcript JSONL schema written to
``~/.claude/projects/<slug>/<session>.jsonl``.

Record ``type`` values seen in real transcripts: ``user``, ``assistant``
(``message.content[]`` items of ``thinking`` / ``text`` / ``tool_use`` /
``tool_result``), ``summary``, ``attachment``, ``last-prompt``,
``queue-operation``. Each non-blank transcript line maps to at most one exhaust
atom carrying a stable per-line id (``ccline_<line>``) so re-parsing / resuming
never duplicates atoms (idempotent by line number).

No network or model calls happen here: the only inputs are the transcript bytes
and the (local SQLite) DAG write path passed in as ``server``.
"""
import hashlib
import json
import os
import re
from pathlib import Path

DEFAULT_MAX_EVENTS = 2000
EXHAUST_ON_VALUES = {"1", "true", "on", "yes"}
ATOM_ID_PREFIX = "ccline"
EXHAUST_HOST = "claude-code-exhaust"
CHECKPOINT_HOST = "claude-code"
EXHAUST_TITLE_SUFFIX = " — exhaust"
MAX_CONTENT_CHARS = 1200

# Mirror the server's sensitive-text guard so previews never materialize secrets.
SENSITIVE_TEXT_RE = re.compile(
    r"(?i)(authorization|bearer\s+[A-Za-z0-9._~+/=-]+|api[_-]?key|secret|password|"
    r"private[_ -]?key|access[_-]?token|refresh[_-]?token)"
)


# --------------------------------------------------------------------------- #
# Config / paths
# --------------------------------------------------------------------------- #
def exhaust_enabled(env=None):
    env = os.environ if env is None else env
    flag = env.get("SCHOLIA_EXHAUST")
    return flag is not None and flag.strip().lower() in EXHAUST_ON_VALUES


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


def claude_projects_dir(env=None):
    env = os.environ if env is None else env
    return Path(env.get("CLAUDE_PROJECTS_DIR") or "~/.claude/projects").expanduser()


def project_slug(cwd):
    """Replicate Claude Code's transcript-directory slug: every non-alphanumeric
    character in the absolute cwd becomes a hyphen."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(cwd))


def transcript_path_for(cwd, session_id, env=None):
    return claude_projects_dir(env) / project_slug(cwd) / f"{session_id}.jsonl"


# --------------------------------------------------------------------------- #
# Pure parser
# --------------------------------------------------------------------------- #
def atom_id_for(line_no):
    return f"{ATOM_ID_PREFIX}_{int(line_no):05d}"


def _sha256(text):
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def _scrub(value, max_chars=MAX_CONTENT_CHARS):
    """Hash + bounded preview of a value, omitting sensitive-looking text."""
    if value is None:
        text = ""
    elif isinstance(value, str):
        text = value
    else:
        text = json.dumps(value, sort_keys=True, separators=(",", ":"))
    record = {"length": len(text), "sha256": _sha256(text)}
    if not text:
        record["text"] = ""
        return record
    if SENSITIVE_TEXT_RE.search(text):
        record["text_omitted_reason"] = "sensitive-looking content"
        return record
    record["text"] = text[:max_chars]
    record["truncated"] = len(text) > max_chars
    return record


def _content_item_types(message):
    content = message.get("content")
    if isinstance(content, list):
        return [item.get("type") for item in content if isinstance(item, dict)]
    return []


def _classify(obj):
    """Return (kind, summary_suffix, extracted) for one parsed transcript record."""
    rtype = obj.get("type", "unknown")
    message = obj.get("message") if isinstance(obj.get("message"), dict) else {}

    if rtype == "assistant":
        item_types = _content_item_types(message)
        if "tool_use" in item_types:
            kind = "Action"
        elif "text" in item_types:
            kind = "Finding"
        elif "thinking" in item_types:
            kind = "Observation"
        else:
            kind = "Observation"
        tool_names, extracted = [], []
        for offset, item in enumerate(message.get("content", []) or []):
            if not isinstance(item, dict):
                continue
            itype = item.get("type")
            if itype == "tool_use":
                tool_names.append(item.get("name") or "tool")
                extracted.append({"type": "tool_use", "name": item.get("name"), "input": _scrub(item.get("input"))})
            elif itype == "text":
                extracted.append({"type": "text", "text": _scrub(item.get("text", ""))})
            elif itype == "thinking":
                extracted.append({"type": "thinking", "thinking": _scrub(item.get("thinking", ""))})
            elif itype == "tool_result":
                extracted.append({"type": "tool_result", "content": _scrub(item.get("content", ""))})
        suffix = " ({})".format(", ".join(t for t in item_types if t)) if item_types else ""
        if tool_names:
            suffix += " -> " + ", ".join(tool_names)
        return kind, suffix, extracted

    if rtype == "user":
        content = message.get("content")
        if isinstance(content, list):
            if any(isinstance(i, dict) and i.get("type") == "tool_result" for i in content):
                extracted = [
                    {"type": "tool_result", "content": _scrub(i.get("content", ""))}
                    for i in content
                    if isinstance(i, dict) and i.get("type") == "tool_result"
                ]
                return "Observation", " (tool_result)", extracted
            extracted = [_scrub(i.get("text", "") if isinstance(i, dict) else i) for i in content]
            return "Question", "", extracted
        return "Question", "", [_scrub(content)]

    if rtype == "summary":
        return "Summary", "", [_scrub(obj.get("summary", ""))]

    # attachment / last-prompt / queue-operation / system / anything else:
    # preserved as a generic Observation so the exhaust trail stays complete.
    return "Observation", f" ({rtype})", []


def _line_to_atom(line_no, raw_line, source=None):
    files = [f"{source}:{line_no}"] if source else []
    try:
        obj = json.loads(raw_line)
    except Exception as exc:  # malformed JSON -> Contradiction, like the importer
        content = json.dumps(
            {"line": line_no, "raw_line_sha256": _sha256(raw_line), "error": str(exc)},
            indent=2,
            sort_keys=True,
        )
        return {
            "atom_id": atom_id_for(line_no),
            "line": line_no,
            "kind": "Contradiction",
            "summary": f"Claude Code event {line_no:05d}: JSON parse error",
            "content": content,
            "files": files,
        }

    if not isinstance(obj, dict):
        content = json.dumps(
            {"line": line_no, "payload_type": f"json_{type(obj).__name__}", "value": _scrub(obj)},
            indent=2,
            sort_keys=True,
        )
        return {
            "atom_id": atom_id_for(line_no),
            "line": line_no,
            "kind": "Observation",
            "summary": f"Claude Code event {line_no:05d}: non-object JSON value",
            "content": content,
            "files": files,
        }

    kind, suffix, extracted = _classify(obj)
    rtype = obj.get("type", "unknown")
    metadata = {
        "line": line_no,
        "type": rtype,
        "raw_line_sha256": _sha256(raw_line),
    }
    message = obj.get("message") if isinstance(obj.get("message"), dict) else {}
    for key in ("uuid", "requestId", "timestamp"):
        if key in obj:
            metadata[key] = obj.get(key)
    if message.get("role"):
        metadata["role"] = message.get("role")
    content = json.dumps({"metadata": metadata, "extracted": extracted}, indent=2, sort_keys=True)
    return {
        "atom_id": atom_id_for(line_no),
        "line": line_no,
        "kind": kind,
        "summary": f"Claude Code event {line_no:05d}: {rtype}{suffix}",
        "content": content,
        "files": files,
    }


class ParseResult:
    __slots__ = ("atoms", "truncated", "scanned")

    def __init__(self, atoms, truncated, scanned):
        self.atoms = atoms
        self.truncated = truncated
        self.scanned = scanned


def parse_transcript_lines(lines, *, max_events=DEFAULT_MAX_EVENTS, start_line=1, source=None):
    """Map transcript lines to exhaust atoms.

    Pure and deterministic. ``max_events`` caps the absolute line number that may
    become an atom (matching the importer's ``raw_lines[:max_events]``), so the
    exhaust DAG never exceeds the cap regardless of how the tailer batches. Lines
    before ``start_line`` are skipped (resume). Blank lines are skipped but keep
    their line numbers, so atom ids remain a stable function of line number.
    """
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
        atoms.append(_line_to_atom(line_no, raw, source=source))
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
    no-op (the server raises ``atom already exists``, which we swallow).
    """
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
    transcript_path,
    dag_id,
    project_path,
    max_events=DEFAULT_MAX_EVENTS,
    start_line=1,
    previous_atom_id=None,
    log=None,
):
    """Read the transcript once, parse from ``start_line``, append new atoms."""
    path = Path(transcript_path)
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return CaptureResult(0, start_line - 1, False, previous_atom_id, start_line - 1)
    parsed = parse_transcript_lines(lines, max_events=max_events, start_line=start_line, source=str(path))
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
    """Find-or-create this session's exhaust DAG, paired to the checkpoint DAG.

    Resolves the checkpoint session DAG (honoring the shared opt-out) to read its
    title, then ensures a sibling exhaust DAG titled ``"<checkpoint> — exhaust"``
    and tagged ``exhaust`` + ``event-source`` so ``trace_view_mode`` resolves to
    ``exhaust`` and ``related_trace_views`` pairs the two. Returns ``None`` when
    auto-emit is opted out (no exhaust capture).
    """
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
            "objective": f"Live Claude Code exhaust trail paired to the {host} checkpoint session.",
        }
    ).get("structuredContent", {})
    return {
        "dag_id": exhaust.get("dag_id"),
        "title": exhaust_title,
        "checkpoint_dag_id": checkpoint.get("dag_id"),
        "checkpoint_title": checkpoint_title,
    }
