#!/usr/bin/env python3
import hashlib
import html
import json
import os
import platform
import re
import secrets
import sqlite3
import sys
import xml.etree.ElementTree as ET
from collections import deque
from datetime import datetime, timezone
from pathlib import Path


# Preferred protocol version = the stable 2026-07-28 release. Older revisions
# stay supported so the adapter is dual-version (PRD mcp-2026-07-28-prd-01):
# pre-handshake-removal hosts keep using ``initialize`` while 2026-07-28 hosts
# use ``server/discover`` + per-request ``_meta`` version carriage.
PROTOCOL_VERSION = "2026-07-28"
SUPPORTED_PROTOCOL_VERSIONS = ("2026-07-28", "2025-11-25", "2025-06-18", "2025-03-26")
SERVER_NAME = "scholialang"
SERVER_VERSION = "0.7.2"

# MCP 2026-07-28 ``_meta`` keys (SEP-2575 / SEP-2322) and the error code for a
# version the server does not support (-32022 per the 2026-07-28 error-code
# allocation policy).
META_PROTOCOL_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CLIENT_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
META_CLIENT_INFO = "io.modelcontextprotocol/clientInfo"
META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"
UNSUPPORTED_PROTOCOL_VERSION = -32022
# CacheableResult hints (SEP-2549) for list/read results. Scope is ``private``:
# this server runs locally against one operator's DAG store, so nothing it
# returns may be shared through a cross-client cache.
CACHEABLE_TTL_MS = 300_000
CACHE_SCOPE = "private"
MIN_VALIDATOR_VERSION = (0, 7, 2)
MAX_TEXT = 6000

# A stdio MCP server can outlive, or be started independently of, a host's
# conversation lifecycle.  When the host does not expose a conversation ID,
# keep implicit calls isolated to this server process instead of collapsing
# every caller into the historical ``unknown:default`` bucket.
RUNTIME_SESSION_ID = "runtime-" + secrets.token_hex(8)


def _validator_version_tuple(value):
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)", str(value))
    if not match:
        return None
    return tuple(int(part) for part in match.groups())


def _has_goal_concluding(atoms_mod):
    kinds = getattr(atoms_mod, "ATOM_KINDS", ())
    version = _validator_version_tuple(
        getattr(atoms_mod, "SCHOLIA_VALIDATOR_VERSION", "")
    )
    return (
        "Goal" in kinds
        and "Concluding" in kinds
        and version is not None
        and version[:2] == MIN_VALIDATOR_VERSION[:2]
        and version >= MIN_VALIDATOR_VERSION
    )


def _load_scholia_engine():
    """Resolve the validator/parser. Prefer the installed scholialang package
    (kept in lockstep with the spec); fall back to the vendored snapshot
    shipped beside this server so the plugin works without a pip install."""
    try:
        from scholialang import validator as _validator_mod  # type: ignore
        from scholialang import parser as _parser_mod  # type: ignore
        from scholialang import atoms as _atoms_mod  # type: ignore
        if _has_goal_concluding(_atoms_mod):
            return _validator_mod, _parser_mod, _atoms_mod, "scholialang-package"
    except ImportError:
        pass
    vendored_dir = Path(__file__).resolve().parent
    if str(vendored_dir) not in sys.path:
        sys.path.insert(0, str(vendored_dir))
    from _scholia_vendored import validator as _validator_mod  # type: ignore
    from _scholia_vendored import parser as _parser_mod  # type: ignore
    from _scholia_vendored import atoms as _atoms_mod  # type: ignore
    return _validator_mod, _parser_mod, _atoms_mod, "scholialang-vendored"


SCHOLIA_VALIDATOR, SCHOLIA_PARSER, SCHOLIA_ATOMS, LINT_ENGINE = _load_scholia_engine()

ATOM_KINDS = list(SCHOLIA_ATOMS.ATOM_KINDS)

_ATOM_SUMMARIES = {
    "Goal": "The target proposition the trace is pursuing.",
    "Hypothesis": "A proposition the agent will test.",
    "Observation": "External input from a command, file, query, or review.",
    "Evidence": "Material that supports, refutes, or qualifies a hypothesis.",
    "Finding": "A conclusion drawn from available evidence.",
    "Concluding": "A premise-backed final or checkpoint conclusion.",
    "Deciding": "A branch point and selected path.",
    "Action": "A durable external state change.",
    "Contradiction": "Two trace claims that cannot both be true.",
    "Retract": "Explicit revocation of a prior finding.",
}

ATOMS = [
    {
        "id": kind.lower(),
        "tag": kind,
        "summary": _ATOM_SUMMARIES.get(kind, f"Canonical Scholia {kind} atom."),
    }
    for kind in ATOM_KINDS
]

OPERATORS = [
    {"token": "AND", "meaning": "Conjunction"},
    {"token": "OR", "meaning": "Inclusive disjunction"},
    {"token": "XOR", "meaning": "Exclusive disjunction"},
    {"token": "NOT", "meaning": "Negation"},
    {"token": "IMPLIES", "meaning": "Entailment or inline implication"},
    {"token": "REFER", "meaning": "Back-link dereference"},
    {"token": "FORALL", "meaning": "Universal quantifier"},
    {"token": "EXISTS", "meaning": "Existential quantifier"},
    {"token": "BEFORE", "meaning": "Temporal ordering"},
    {"token": "AFTER", "meaning": "Temporal ordering"},
    {"token": "EQUALS", "meaning": "Value or identity equality"},
]

RELATIONS = [
    "refers",
    "supports",
    "refutes",
    "neutral",
    "implies",
    "depends_on",
    "derived_from",
    "contradicts",
    "retracts",
    "before",
    "after",
]

RESOURCE_TEXT = {
    "scholialang://local-guide": """# Scholialang Local SQLite DAG

The local Scholialang server stores traces as DAGs in SQLite. Atoms are nodes.
Explicit references, evidential links, implications, contradictions, and
retractions are directed edges.

Use summaries, frontiers, and neighborhood reads before requesting full graph
exports. This keeps Codex context focused while preserving a richer local
history on disk.""",
    "scholialang://atoms": json.dumps(ATOMS, indent=2),
    "scholialang://operators": json.dumps(OPERATORS, indent=2),
    "scholialang://relations": json.dumps(RELATIONS, indent=2),
}


def now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def storage_root():
    return Path(os.environ.get("SCHOLIALANG_HOME", "~/.scholialang")).expanduser()


def database_path():
    return storage_root() / "scholialang.sqlite3"


def connect():
    path = database_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    # Two hosts (e.g. Claude Code + Codex) may write the same shared DB at once.
    # WAL serializes writers; busy_timeout makes the second writer wait instead
    # of raising "database is locked".
    conn.execute("PRAGMA busy_timeout = 5000")
    init_db(conn)
    return conn


def _ensure_column(conn, table, column, decl):
    """Add a column to an existing table if missing (idempotent migration)."""
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def init_db(conn):
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS projects (
          project_key TEXT PRIMARY KEY,
          project_path TEXT,
          project_name TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS dags (
          dag_id TEXT PRIMARY KEY,
          title TEXT NOT NULL,
          objective TEXT NOT NULL,
          tags_json TEXT NOT NULL,
          project_key TEXT NOT NULL,
          project_path TEXT,
          project_name TEXT NOT NULL,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          session_key TEXT,
          FOREIGN KEY (project_key) REFERENCES projects(project_key) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS counters (
          dag_id TEXT NOT NULL,
          kind TEXT NOT NULL,
          value INTEGER NOT NULL,
          PRIMARY KEY (dag_id, kind),
          FOREIGN KEY (dag_id) REFERENCES dags(dag_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS nodes (
          dag_id TEXT NOT NULL,
          atom_id TEXT NOT NULL,
          ordinal INTEGER NOT NULL,
          kind TEXT NOT NULL,
          summary TEXT NOT NULL,
          content TEXT NOT NULL,
          files_json TEXT NOT NULL,
          confidence_json TEXT,
          attrs_json TEXT NOT NULL DEFAULT '{}',
          created_at TEXT NOT NULL,
          PRIMARY KEY (dag_id, atom_id),
          FOREIGN KEY (dag_id) REFERENCES dags(dag_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS edges (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          dag_id TEXT NOT NULL,
          from_atom_id TEXT NOT NULL,
          to_atom_id TEXT NOT NULL,
          relation TEXT NOT NULL,
          label TEXT NOT NULL,
          created_at TEXT NOT NULL,
          UNIQUE (dag_id, from_atom_id, to_atom_id, relation, label),
          FOREIGN KEY (dag_id) REFERENCES dags(dag_id) ON DELETE CASCADE,
          FOREIGN KEY (dag_id, from_atom_id) REFERENCES nodes(dag_id, atom_id) ON DELETE CASCADE,
          FOREIGN KEY (dag_id, to_atom_id) REFERENCES nodes(dag_id, atom_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS summaries (
          dag_id TEXT PRIMARY KEY,
          markdown TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          FOREIGN KEY (dag_id) REFERENCES dags(dag_id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS session_bindings (
          project_key TEXT NOT NULL,
          host TEXT NOT NULL,
          runtime_scope TEXT NOT NULL,
          session_key TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          PRIMARY KEY (project_key, host, runtime_scope),
          FOREIGN KEY (project_key) REFERENCES projects(project_key) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_dags_project_updated ON dags(project_key, updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_nodes_dag_ordinal ON nodes(dag_id, ordinal);
        CREATE INDEX IF NOT EXISTS idx_edges_dag_from ON edges(dag_id, from_atom_id);
        CREATE INDEX IF NOT EXISTS idx_edges_dag_to ON edges(dag_id, to_atom_id);
        """
    )
    # Migrate pre-0.3.0 databases and back the idempotent session lookup.
    _ensure_column(conn, "dags", "session_key", "TEXT")
    _ensure_column(conn, "dags", "model", "TEXT")
    _ensure_column(conn, "dags", "orchestrator", "TEXT")
    _ensure_column(conn, "nodes", "attrs_json", "TEXT NOT NULL DEFAULT '{}'")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_dags_session ON dags(project_key, session_key)")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_dags_session_unique "
        "ON dags(project_key, session_key) WHERE session_key IS NOT NULL"
    )


def compact_text(value, limit=MAX_TEXT):
    if value is None:
        return ""
    text = str(value)
    if len(text) <= limit:
        return text
    return text[:limit] + "\n[truncated]"


def json_loads(value, default):
    if value is None:
        return default
    try:
        return json.loads(value)
    except Exception:
        return default


def json_dumps(value):
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def project_info(project_path=None):
    if project_path:
        resolved = Path(project_path).expanduser().resolve(strict=False)
        raw = str(resolved)
        name = resolved.name or "workspace"
    else:
        raw = "global"
        name = "global"
    key = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return {"project_path": None if raw == "global" else raw, "project_name": name, "project_key": key}


def upsert_project(conn, info):
    timestamp = now()
    conn.execute(
        """
        INSERT INTO projects (project_key, project_path, project_name, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(project_key) DO UPDATE SET
          project_path = excluded.project_path,
          project_name = excluded.project_name,
          updated_at = excluded.updated_at
        """,
        (info["project_key"], info["project_path"], info["project_name"], timestamp, timestamp),
    )


MODEL_TAG_PREFIX = "model:"
ORCHESTRATOR_TAG_PREFIX = "orchestrator:"


def _is_machine_identity(value):
    """True when ``value`` is just this machine's name.

    A hostname says where something ran, never what ran it. Callers given one
    slot for two facts have put a machine name in ``host`` before, which reads
    as a harness named after a laptop and silently loses the real one.
    Recording nothing beats recording that, so identity fields drop it.
    """
    if not value:
        return False
    node = platform.node() or ""
    candidates = {node.lower(), node.split(".", 1)[0].lower()} - {""}
    return value.strip().lower() in candidates


def dag_orchestrator(dag):
    """What drove the harness, when something else did.

    ``harness`` is what ran the model (``claude-code``); this is whatever
    invoked the harness — a CI job, a scheduler, an outer agent framework. It
    is declared, never guessed: a wrapper exports ``SCHOLIA_ORCHESTRATOR`` or
    passes ``orchestrator=``, and an ``orchestrator:<id>`` tag is the fallback
    for externally-ingested traces. Absent means nobody claimed it, which is a
    truthful answer rather than a guessed one.
    """
    stored = dag.get("orchestrator")
    if stored:
        return stored
    for tag in dag.get("tags", []) or []:
        if isinstance(tag, str) and tag.startswith(ORCHESTRATOR_TAG_PREFIX):
            value = tag[len(ORCHESTRATOR_TAG_PREFIX):].strip()
            if value and not _is_machine_identity(value):
                return value
    return None


def requested_orchestrator(args):
    """Resolve a declared orchestrator from the call or the environment."""
    value = args.get("orchestrator") or os.environ.get("SCHOLIA_ORCHESTRATOR")
    if not value or _is_machine_identity(value):
        return None
    return compact_text(value, 120)


def set_dag_orchestrator(conn, dag_id, orchestrator, *, overwrite=False):
    """Stamp the orchestrator on a DAG. First writer wins unless ``overwrite``."""
    if not orchestrator or _is_machine_identity(orchestrator):
        return None
    value = compact_text(orchestrator, 120)
    if not overwrite:
        row = conn.execute("SELECT orchestrator FROM dags WHERE dag_id = ?", (dag_id,)).fetchone()
        if row is not None and ("orchestrator" in row.keys()) and row["orchestrator"]:
            return row["orchestrator"]
    conn.execute("UPDATE dags SET orchestrator = ? WHERE dag_id = ?", (value, dag_id))
    return value
EXHAUST_HOST_SUFFIX = "-exhaust"


def dag_harness(dag):
    """The harness that produced a trace, independent of stream kind.

    ``session_key`` hosts encode both facts at once: ``claude-code`` for the
    checkpoint stream and ``claude-code-exhaust`` for its paired event stream.
    Exhaust-ness is a view mode (see the viewer's ``trace_view_mode``), not a
    different harness, so anything asking "who produced this" wants the bare
    name — both halves of a pair answer ``claude-code``.
    """
    host = (dag.get("session_key") or "").split(":", 1)[0]
    if host.endswith(EXHAUST_HOST_SUFFIX):
        host = host[: -len(EXHAUST_HOST_SUFFIX)]
    return host or None


def dag_model(dag):
    """Resolve the model that produced ``dag``.

    The stored column wins. A ``model:<id>`` tag is the fallback so traces
    ingested from outside the hook path — imports, other harnesses, manual
    replays — can declare provenance without a schema write.
    """
    stored = dag.get("model")
    if stored:
        return stored
    for tag in dag.get("tags", []) or []:
        if isinstance(tag, str) and tag.startswith(MODEL_TAG_PREFIX):
            value = tag[len(MODEL_TAG_PREFIX):].strip()
            if value:
                return value
    return None


def set_dag_model(conn, dag_id, model, *, overwrite=False):
    """Stamp the model on a DAG. Idempotent: first writer wins unless
    ``overwrite``, so a mid-session model switch does not rewrite the
    provenance of atoms already recorded under the earlier model.
    """
    value = compact_text(model or "", 120)
    if not value:
        return None
    if not overwrite:
        row = conn.execute("SELECT model FROM dags WHERE dag_id = ?", (dag_id,)).fetchone()
        if row is not None and ("model" in row.keys()) and row["model"]:
            return row["model"]
    conn.execute("UPDATE dags SET model = ? WHERE dag_id = ?", (value, dag_id))
    return value


def dag_metadata(dag):
    node_count = dag.get("node_count")
    if node_count is None:
        node_count = len(dag.get("nodes", {}))
    edge_count = dag.get("edge_count")
    if edge_count is None:
        edge_count = len(dag.get("edges", []))
    return {
        "dag_id": dag["dag_id"],
        "trace_id": dag["dag_id"],
        "title": dag.get("title", ""),
        "objective": dag.get("objective", ""),
        "tags": dag.get("tags", []),
        "session_key": dag.get("session_key"),
        "model": dag_model(dag),
        "host": (dag.get("session_key") or "").split(":", 1)[0] or None,
        "harness": dag_harness(dag),
        "orchestrator": dag_orchestrator(dag),
        "project_path": dag.get("project_path"),
        "project_name": dag.get("project_name"),
        "project_key": dag.get("project_key"),
        "created_at": dag.get("created_at"),
        "updated_at": dag.get("updated_at"),
        "node_count": node_count,
        "edge_count": edge_count,
        "database_path": str(database_path()),
    }


def row_to_dag(conn, row):
    nodes = {}
    order = []
    for node in conn.execute(
        "SELECT * FROM nodes WHERE dag_id = ? ORDER BY ordinal ASC",
        (row["dag_id"],),
    ):
        atom = {
            "id": node["atom_id"],
            "kind": node["kind"],
            "summary": node["summary"],
            "content": node["content"],
            "files": json_loads(node["files_json"], []),
            "confidence": json_loads(node["confidence_json"], None),
            "attributes": json_loads(node["attrs_json"], {}) if "attrs_json" in node.keys() else {},
            "created_at": node["created_at"],
        }
        nodes[node["atom_id"]] = atom
        order.append(node["atom_id"])

    edges = []
    for edge in conn.execute(
        "SELECT * FROM edges WHERE dag_id = ? ORDER BY id ASC",
        (row["dag_id"],),
    ):
        value = {
            "from": edge["from_atom_id"],
            "to": edge["to_atom_id"],
            "relation": edge["relation"],
            "created_at": edge["created_at"],
        }
        if edge["label"]:
            value["label"] = edge["label"]
        edges.append(value)

    counters = {
        counter["kind"]: counter["value"]
        for counter in conn.execute("SELECT * FROM counters WHERE dag_id = ?", (row["dag_id"],))
    }

    return {
        "type": "scholialang.local_sqlite_dag",
        "dag_id": row["dag_id"],
        "title": row["title"],
        "objective": row["objective"],
        "tags": json_loads(row["tags_json"], []),
        "session_key": row["session_key"] if "session_key" in row.keys() else None,
        "model": row["model"] if "model" in row.keys() else None,
        "orchestrator": row["orchestrator"] if "orchestrator" in row.keys() else None,
        "project_key": row["project_key"],
        "project_path": row["project_path"],
        "project_name": row["project_name"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "nodes": nodes,
        "edges": edges,
        "order": order,
        "counters": counters,
    }


def load_dag_conn(conn, dag_id, project_path=None):
    params = [dag_id]
    where = "dag_id = ?"
    if project_path:
        info = project_info(project_path)
        where += " AND project_key = ?"
        params.append(info["project_key"])
    row = conn.execute(f"SELECT * FROM dags WHERE {where}", params).fetchone()
    if row is None:
        raise ValueError(f"dag not found: {dag_id}")
    return row_to_dag(conn, row)


def load_dag(dag_id, project_path=None):
    conn = connect()
    try:
        return load_dag_conn(conn, dag_id, project_path)
    finally:
        conn.close()


def touch_dag(conn, dag_id):
    timestamp = now()
    row = conn.execute("SELECT project_key FROM dags WHERE dag_id = ?", (dag_id,)).fetchone()
    conn.execute("UPDATE dags SET updated_at = ? WHERE dag_id = ?", (timestamp, dag_id))
    if row:
        conn.execute("UPDATE projects SET updated_at = ? WHERE project_key = ?", (timestamp, row["project_key"]))


def require_str(args, key):
    value = args.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value.strip()


def optional_list(args, key):
    value = args.get(key, [])
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"{key} must be an array")
    return [str(item) for item in value]


def normalize_kind(kind):
    raw = str(kind or "Finding").strip()
    aliases = {
        "conclusion": "Concluding",
        "concluding": "Concluding",
        "decision": "Deciding",
        "deciding": "Deciding",
        "goal": "Goal",
        "retraction": "Retract",
        "retract": "Retract",
    }
    if raw.lower() in aliases:
        return aliases[raw.lower()]
    for known in ATOM_KINDS:
        if raw.lower() == known.lower():
            return known
    raise ValueError(f"unknown Scholia atom kind: {raw}")


def normalize_attributes(kind, value):
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("attributes must be an object")
    allowed_by_kind = getattr(SCHOLIA_PARSER, "_ALLOWED_ATTRS_BY_KIND", {})
    allowed = set(allowed_by_kind.get(kind, ())) - {"id", "value"}
    normalized = {}
    for key, raw_value in value.items():
        key = str(key)
        if key not in allowed:
            raise ValueError(
                f"unknown attribute {key!r} for {kind}; allowed attributes: {sorted(allowed)}"
            )
        if raw_value is None:
            continue
        if isinstance(raw_value, list):
            normalized[key] = [str(item) for item in raw_value]
        elif isinstance(raw_value, (str, int, float, bool)):
            normalized[key] = str(raw_value).lower() if isinstance(raw_value, bool) else str(raw_value)
        else:
            raise ValueError(f"attribute {key!r} must be a scalar or array")
    return normalized


def dag_id_arg(args):
    value = args.get("dag_id") or args.get("trace_id")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("dag_id must be a non-empty string")
    return value.strip()


def new_atom_id(conn, dag_id, kind):
    row = conn.execute(
        "SELECT value FROM counters WHERE dag_id = ? AND kind = ?",
        (dag_id, kind),
    ).fetchone()
    value = int(row["value"]) + 1 if row else 1
    conn.execute(
        """
        INSERT INTO counters (dag_id, kind, value)
        VALUES (?, ?, ?)
        ON CONFLICT(dag_id, kind) DO UPDATE SET value = excluded.value
        """,
        (dag_id, kind, value),
    )
    return f"{kind}_{value:04d}"


def adjacency(dag):
    graph = {}
    for edge in dag.get("edges", []):
        graph.setdefault(edge["from"], set()).add(edge["to"])
    return graph


def path_exists(graph, start, target):
    seen = set()
    queue = deque([start])
    while queue:
        node = queue.popleft()
        if node == target:
            return True
        if node in seen:
            continue
        seen.add(node)
        queue.extend(graph.get(node, ()))
    return False


def add_edge(conn, dag, from_id, to_id, relation="refers", label=""):
    if from_id not in dag["nodes"]:
        raise ValueError(f"unknown from atom: {from_id}")
    if to_id not in dag["nodes"]:
        raise ValueError(f"unknown to atom: {to_id}")
    if from_id == to_id:
        raise ValueError("self edges are not allowed in the local DAG")
    relation = str(relation or "refers").strip() or "refers"
    label = compact_text(label or "", 300)
    graph = adjacency(dag)
    if path_exists(graph, to_id, from_id):
        raise ValueError(f"edge would create a cycle: {from_id} -> {to_id}")
    edge = {"from": from_id, "to": to_id, "relation": relation, "created_at": now()}
    if label:
        edge["label"] = label
    conn.execute(
        """
        INSERT OR IGNORE INTO edges (dag_id, from_atom_id, to_atom_id, relation, label, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (dag["dag_id"], from_id, to_id, relation, label, edge["created_at"]),
    )
    if edge not in dag["edges"]:
        dag["edges"].append(edge)
    return edge


def graph_degrees(dag):
    incoming = {node_id: 0 for node_id in dag.get("nodes", {})}
    outgoing = {node_id: 0 for node_id in dag.get("nodes", {})}
    for edge in dag.get("edges", []):
        outgoing[edge["from"]] = outgoing.get(edge["from"], 0) + 1
        incoming[edge["to"]] = incoming.get(edge["to"], 0) + 1
    return incoming, outgoing


def frontier_nodes(dag, kind_filter=None):
    incoming, outgoing = graph_degrees(dag)
    kinds = set(kind_filter or [])
    frontier = []
    for node_id in reversed(dag.get("order", [])):
        node = dag["nodes"].get(node_id)
        if not node:
            continue
        if incoming.get(node_id, 0) == 0 and (not kinds or node.get("kind") in kinds):
            frontier.append({**node, "incoming": incoming.get(node_id, 0), "outgoing": outgoing.get(node_id, 0)})
    return frontier


def content_result(text, structured=None, is_error=False):
    result = {"content": [{"type": "text", "text": text}], "isError": is_error}
    if structured is not None:
        result["structuredContent"] = structured
    return result


def tool_dag_start(args):
    conn = connect()
    try:
        info = project_info(args.get("project_path"))
        upsert_project(conn, info)
        dag_id = "dag_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + secrets.token_hex(4)
        timestamp = now()
        conn.execute(
            """
            INSERT INTO dags (
              dag_id, title, objective, tags_json, project_key, project_path,
              project_name, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                dag_id,
                compact_text(args.get("title") or "Untitled Scholialang DAG", 200),
                compact_text(args.get("objective", ""), 1000),
                json_dumps(optional_list(args, "tags")),
                info["project_key"],
                info["project_path"],
                info["project_name"],
                timestamp,
                timestamp,
            ),
        )
        set_dag_model(conn, dag_id, args.get("model"))
        set_dag_orchestrator(conn, dag_id, requested_orchestrator(args))
        conn.commit()
        goal_summary = compact_text(args.get("objective") or args.get("title") or "Trace objective", 500)
        goal_content = compact_text(args.get("objective") or goal_summary, MAX_TEXT)
        goal_atom = tool_dag_add_atom(
            {
                "dag_id": dag_id,
                "project_path": info["project_path"],
                "kind": "Goal",
                "summary": goal_summary,
                "content": goal_content,
            }
        )["structuredContent"]["atom"]
        dag = load_dag_conn(conn, dag_id)
        structured = dag_metadata(dag)
        structured["goal_atom"] = goal_atom
        return content_result(f"Started Scholialang SQLite DAG {dag_id} for {info['project_name']}.", structured)
    finally:
        conn.close()


def tool_dag_set_model(args):
    """Record the model that produced a DAG.

    Separate from dag_start because a Claude Code transcript carries no
    assistant message at SessionStart — the model only becomes knowable once
    the first assistant turn lands, so the tailer backfills it then.
    """
    dag_id = dag_id_arg(args)
    conn = connect()
    try:
        dag = load_dag_conn(conn, dag_id, args.get("project_path"))
        value = set_dag_model(conn, dag_id, require_str(args, "model"), overwrite=bool(args.get("overwrite")))
        conn.commit()
        structured = {"dag_id": dag_id, "trace_id": dag_id, "model": value or dag_model(dag)}
        return content_result(f"Model for {dag_id} is {structured['model']}.", structured)
    finally:
        conn.close()


def tool_dag_add_atom(args):
    dag_id = dag_id_arg(args)
    conn = connect()
    try:
        # Reserve the single SQLite writer before reading graph state. Cycle
        # validation, atom-id allocation, ordinal allocation, and persistence
        # must observe one serialized snapshot across concurrent hosts.
        conn.execute("BEGIN IMMEDIATE")
        dag = load_dag_conn(conn, dag_id, args.get("project_path"))
        kind = normalize_kind(args.get("kind"))
        atom_id = args.get("atom_id") or new_atom_id(conn, dag_id, kind)
        if atom_id in dag["nodes"]:
            raise ValueError(f"atom already exists: {atom_id}")
        node = {
            "id": atom_id,
            "kind": kind,
            "summary": compact_text(require_str(args, "summary"), 500),
            "content": compact_text(args.get("content", ""), MAX_TEXT),
            "files": optional_list(args, "files"),
            "confidence": args.get("confidence"),
            "attributes": normalize_attributes(kind, args.get("attributes")),
            "created_at": now(),
        }
        ordinal = len(dag["order"]) + 1
        conn.execute(
            """
            INSERT INTO nodes (
              dag_id, atom_id, ordinal, kind, summary, content,
              files_json, confidence_json, attrs_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                dag_id,
                atom_id,
                ordinal,
                kind,
                node["summary"],
                node["content"],
                json_dumps(node["files"]),
                json_dumps(node["confidence"]) if node["confidence"] is not None else None,
                json_dumps(node["attributes"]),
                node["created_at"],
            ),
        )
        dag["nodes"][atom_id] = node
        dag["order"].append(atom_id)

        created_edges = []
        for ref in optional_list(args, "refs"):
            created_edges.append(add_edge(conn, dag, atom_id, ref, "refers"))
        for link in args.get("links", []) or []:
            if not isinstance(link, dict):
                raise ValueError("links must contain objects")
            created_edges.append(
                add_edge(conn, dag, atom_id, require_str(link, "to"), link.get("relation", "refers"), link.get("label", ""))
            )

        touch_dag(conn, dag_id)
        conn.commit()
        structured = {"dag_id": dag_id, "trace_id": dag_id, "atom": node, "edges": created_edges, "database_path": str(database_path())}
        return content_result(f"Added {atom_id} to {dag_id}.", structured)
    finally:
        conn.close()


def tool_dag_link(args):
    dag_id = dag_id_arg(args)
    conn = connect()
    try:
        # Reading before taking the write lock lets two callers approve
        # opposite edges from the same stale graph and persist a cycle.
        conn.execute("BEGIN IMMEDIATE")
        dag = load_dag_conn(conn, dag_id, args.get("project_path"))
        edge = add_edge(
            conn,
            dag,
            require_str(args, "from"),
            require_str(args, "to"),
            args.get("relation", "refers"),
            args.get("label", ""),
        )
        touch_dag(conn, dag_id)
        conn.commit()
        return content_result(f"Linked {edge['from']} -> {edge['to']} ({edge['relation']}).", {"dag_id": dag_id, "edge": edge})
    finally:
        conn.close()


def autoemit_disabled_reason(project_path=None):
    """Return a short reason string if auto-emit is OFF, else None.

    One opt-out, honored identically by every host because every host calls
    this same server. Off wins: either the SCHOLIA_AUTOEMIT env switch or a
    per-project .scholia-off marker disables the auto path. Explicit,
    user-driven tracing (auto=False) is never gated here.
    """
    flag = os.environ.get("SCHOLIA_AUTOEMIT")
    if flag is not None and flag.strip().lower() in {"0", "false", "off", "no"}:
        return "env:SCHOLIA_AUTOEMIT"
    if project_path:
        try:
            if (Path(project_path).expanduser() / ".scholia-off").exists():
                return "file:.scholia-off"
        except OSError:
            pass
    return None


def autoemit_enabled(project_path=None):
    return autoemit_disabled_reason(project_path) is None


def runtime_scope_id():
    """Return the host-process scope shared by sibling hook/MCP children."""
    return compact_text(
        os.environ.get("SCHOLIA_RUNTIME_ID") or f"parent-pid-{os.getppid()}",
        200,
    )


def requested_session_identity(args):
    """Resolve identity supplied by a caller or host environment."""
    requested_host = args.get("host") or os.environ.get("SCHOLIA_HOST")
    if _is_machine_identity(requested_host):
        # A caller naming the machine has not named a harness. Fall back to the
        # generic default rather than minting a harness called after a laptop.
        requested_host = None
    host = compact_text(requested_host or "mcp", 60)
    raw_session_id = (
        args.get("session_id")
        or os.environ.get("SCHOLIA_SESSION_ID")
        or os.environ.get("CLAUDE_SESSION_ID")
    )
    session_id = compact_text(raw_session_id, 200) if raw_session_id else None
    return host, session_id


def find_bound_session_id(conn, project_key, host):
    row = conn.execute(
        "SELECT session_key FROM session_bindings "
        "WHERE project_key = ? AND host = ? AND runtime_scope = ?",
        (project_key, host, runtime_scope_id()),
    ).fetchone()
    if row is None:
        return None
    prefix = f"{host}:"
    session_key = row["session_key"]
    return session_key[len(prefix):] if session_key.startswith(prefix) else None


def bind_runtime_session(conn, project_key, host, session_key):
    conn.execute(
        """
        INSERT INTO session_bindings (
          project_key, host, runtime_scope, session_key, updated_at
        ) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(project_key, host, runtime_scope) DO UPDATE SET
          session_key = excluded.session_key,
          updated_at = excluded.updated_at
        """,
        (project_key, host, runtime_scope_id(), session_key, now()),
    )


def clear_runtime_session_binding(conn, project_key, host, session_key):
    conn.execute(
        "DELETE FROM session_bindings "
        "WHERE project_key = ? AND host = ? AND runtime_scope = ? AND session_key = ?",
        (project_key, host, runtime_scope_id(), session_key),
    )


def resolve_session_identity(args, conn=None, info=None):
    """Resolve a stable identity without a cross-host shared fallback.

    Explicit tool arguments are authoritative.  Hosts may then provide a
    stable identity through SCHOLIA_* variables (CLAUDE_SESSION_ID remains a
    compatibility input for Claude Code wrappers).  If neither is available,
    the fallback is unique to this MCP server process and remains idempotent
    for its lifetime.
    """
    host, session_id = requested_session_identity(args)
    if session_id is None and conn is not None and info is not None:
        session_id = find_bound_session_id(conn, info["project_key"], host)
    session_id = session_id or RUNTIME_SESSION_ID
    return host, session_id


def session_key_for(host, session_id):
    return f"{host}:{session_id}"


def find_session_dag_row(conn, project_key, session_key):
    return conn.execute(
        "SELECT * FROM dags WHERE project_key = ? AND session_key = ? ORDER BY created_at ASC LIMIT 1",
        (project_key, session_key),
    ).fetchone()


def tool_dag_ensure_session(args):
    """Idempotent find-or-create of the current session's per-project DAG.

    Keyed by (project, host, session_id) so re-fires (resume/compact) return
    the same DAG, and a Claude Code session and a Codex session on the same
    repo get distinct, host-tagged traces. Gated by the shared opt-out when
    auto=True; explicit calls (auto=False) always create.
    """
    project_path = args.get("project_path")
    host, requested_session_id = requested_session_identity(args)
    session_id = requested_session_id or RUNTIME_SESSION_ID
    auto = args.get("auto", True)
    session_key = session_key_for(host, session_id)

    if auto:
        reason = autoemit_disabled_reason(project_path)
        if reason is not None:
            structured = {
                "enabled": False,
                "created": False,
                "skipped": True,
                "reason": reason,
                "host": host,
                "session_id": session_id,
                "session_key": session_key,
            }
            return content_result(f"Auto-emit disabled ({reason}); no session DAG created.", structured)

    conn = connect()
    try:
        info = project_info(project_path)
        upsert_project(conn, info)
        if requested_session_id is None:
            session_id = find_bound_session_id(conn, info["project_key"], host) or RUNTIME_SESSION_ID
            session_key = session_key_for(host, session_id)
        existing = find_session_dag_row(conn, info["project_key"], session_key)
        if existing is None:
            dag_id = "dag_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + secrets.token_hex(4)
            timestamp = now()
            tags = optional_list(args, "tags")
            merged_tags = list(dict.fromkeys([*tags, f"host:{host}", f"session:{session_id}", "autoemit"]))
            title = compact_text(args.get("title") or f"{info['project_name']} session ({host})", 200)
            objective = compact_text(
                args.get("objective") or f"Auto-emitted session trace for {info['project_name']} via {host}.",
                1000,
            )
            try:
                conn.execute(
                    """
                    INSERT INTO dags (
                      dag_id, title, objective, tags_json, project_key, project_path,
                      project_name, created_at, updated_at, session_key
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        dag_id,
                        title,
                        objective,
                        json_dumps(merged_tags),
                        info["project_key"],
                        info["project_path"],
                        info["project_name"],
                        timestamp,
                        timestamp,
                        session_key,
                    ),
                )
                if requested_session_id is not None:
                    bind_runtime_session(conn, info["project_key"], host, session_key)
                set_dag_model(conn, dag_id, args.get("model"))
                set_dag_orchestrator(conn, dag_id, requested_orchestrator(args))
                conn.commit()
            except sqlite3.IntegrityError:
                # Another host/process created the same session DAG concurrently.
                conn.rollback()
                existing = find_session_dag_row(conn, info["project_key"], session_key)
            else:
                goal_atom = tool_dag_add_atom(
                    {
                        "dag_id": dag_id,
                        "project_path": info["project_path"],
                        "kind": "Goal",
                        "summary": objective,
                        "content": objective,
                    }
                )["structuredContent"]["atom"]
                dag = load_dag_conn(conn, dag_id)
                structured = dag_metadata(dag)
                structured.update(
                    {"enabled": True, "created": True, "host": host, "session_id": session_id, "goal_atom": goal_atom}
                )
                return content_result(
                    f"Started session DAG {dag_id} for {info['project_name']} ({host}).", structured
                )

        # A watcher or lifecycle hook may have created the session before the
        # harness learned its model or orchestrator. Backfill missing values
        # on resume while preserving the existing first-writer-wins contract.
        set_dag_model(conn, existing["dag_id"], args.get("model"))
        set_dag_orchestrator(conn, existing["dag_id"], requested_orchestrator(args))
        if requested_session_id is not None:
            bind_runtime_session(conn, info["project_key"], host, session_key)
        conn.commit()
        existing = find_session_dag_row(conn, info["project_key"], session_key)
        dag = row_to_dag(conn, existing)
        structured = dag_metadata(dag)
        structured.update({"enabled": True, "created": False, "host": host, "session_id": session_id})
        return content_result(
            f"Resumed session DAG {dag['dag_id']} for {info['project_name']} ({host}).", structured
        )
    finally:
        conn.close()


def tool_dag_finish_session(args):
    """Record session termination, optionally closing its goal.

    Re-derives the DAG from (project, host, session_id) so the caller need not
    track the dag_id. Safe no-op when no session DAG exists (e.g. opt-out was
    active at session start). A lifecycle event alone does not prove goal
    attainment: callers must supply outcome=met|unmet|partially_met to create
    a goal-closing Concluding atom. Without an outcome, the default is an
    Observation that records only that the session ended.
    """
    project_path = args.get("project_path")
    host, _ = requested_session_identity(args)
    conn = connect()
    try:
        info = project_info(project_path)
        host, session_id = resolve_session_identity(args, conn, info)
        session_key = session_key_for(host, session_id)
        existing = find_session_dag_row(conn, info["project_key"], session_key)
        dag_id = existing["dag_id"] if existing is not None else None
        project_path_resolved = info["project_path"]
    finally:
        conn.close()

    if dag_id is None:
        structured = {"found": False, "host": host, "session_id": session_id, "session_key": session_key}
        return content_result("No session DAG to finish.", structured)

    outcome = args.get("outcome")
    if outcome is not None:
        outcome = str(outcome).strip()
        if outcome not in {"met", "unmet", "partially_met"}:
            raise ValueError("outcome must be one of: met, unmet, partially_met")
    kind = normalize_kind(args.get("kind") or ("Concluding" if outcome else "Observation"))
    if kind == "Concluding" and outcome is None:
        raise ValueError("outcome is required when finishing a session with Concluding")
    if kind != "Concluding" and outcome is not None:
        raise ValueError("outcome is only valid when finishing a session with Concluding")
    summary_text = args.get("summary") or "Session ended."
    dag = load_dag(dag_id, project_path_resolved)
    goal_ids = [node_id for node_id in dag.get("order", []) if dag["nodes"][node_id].get("kind") == "Goal"]
    attributes = {}
    links = []
    if kind == "Concluding" and goal_ids:
        attributes = {"for_goal": goal_ids[0], "status": outcome}
        links.append({"to": goal_ids[0], "relation": "derived_from", "label": "session goal closure"})
        premise_ids = [
            node_id
            for node_id in dag.get("order", [])
            if dag["nodes"][node_id].get("kind") in {"Finding", "Observation", "Evidence"}
        ]
        if premise_ids:
            links.append({"to": premise_ids[-1], "relation": "derived_from", "label": "session closing premise"})
    atom = tool_dag_add_atom(
        {
            "dag_id": dag_id,
            "project_path": project_path_resolved,
            "kind": kind,
            "summary": compact_text(summary_text, 500),
            "content": compact_text(summary_text, MAX_TEXT),
            "attributes": attributes,
            "links": links,
        }
    )["structuredContent"]["atom"]
    conn = connect()
    try:
        clear_runtime_session_binding(conn, info["project_key"], host, session_key)
        conn.commit()
    finally:
        conn.close()
    structured = {
        "found": True,
        "dag_id": dag_id,
        "atom": atom,
        "host": host,
        "session_id": session_id,
        "outcome": outcome,
    }
    return content_result(f"Finished session DAG {dag_id} ({host}).", structured)


def all_dags(project_path=None):
    conn = connect()
    try:
        params = []
        where = ""
        if project_path:
            info = project_info(project_path)
            where = "WHERE project_key = ?"
            params.append(info["project_key"])
        rows = conn.execute(f"SELECT * FROM dags {where} ORDER BY updated_at DESC", params).fetchall()
        return [row_to_dag(conn, row) for row in rows]
    finally:
        conn.close()


def tool_dag_list(args):
    limit = int(args.get("limit", 10))
    limit = max(0, limit)
    conn = connect()
    try:
        params = []
        where = ""
        if args.get("project_path"):
            info = project_info(args.get("project_path"))
            where = "WHERE d.project_key = ?"
            params.append(info["project_key"])
        params.append(limit)
        rows = conn.execute(
            f"""
            SELECT d.*,
              (SELECT COUNT(*) FROM nodes n WHERE n.dag_id = d.dag_id) AS node_count,
              (SELECT COUNT(*) FROM edges e WHERE e.dag_id = d.dag_id) AS edge_count
            FROM dags d
            {where}
            ORDER BY d.updated_at DESC
            LIMIT ?
            """,
            params,
        ).fetchall()
        items = [dag_metadata(dict(row)) for row in rows]
    finally:
        conn.close()
    structured = {"dags": items, "database_path": str(database_path())}
    return content_result(json.dumps(structured, indent=2, sort_keys=True), structured)


def neighborhood(dag, atom_id, depth=1, direction="both"):
    if atom_id not in dag["nodes"]:
        raise ValueError(f"unknown atom: {atom_id}")
    outgoing = {}
    incoming = {}
    for edge in dag.get("edges", []):
        outgoing.setdefault(edge["from"], []).append((edge["to"], edge))
        incoming.setdefault(edge["to"], []).append((edge["from"], edge))
    seen = {atom_id}
    edges = []
    queue = deque([(atom_id, 0)])
    while queue:
        node_id, distance = queue.popleft()
        if distance >= depth:
            continue
        candidates = []
        if direction in {"outgoing", "both"}:
            candidates.extend(outgoing.get(node_id, []))
        if direction in {"incoming", "both"}:
            candidates.extend(incoming.get(node_id, []))
        for next_id, edge in candidates:
            edges.append(edge)
            if next_id not in seen:
                seen.add(next_id)
                queue.append((next_id, distance + 1))
    ordered_nodes = [dag["nodes"][node_id] for node_id in dag.get("order", []) if node_id in seen]
    return {"nodes": ordered_nodes, "edges": edges}


def build_summary(dag, max_items=8, focus_atom_id=None):
    counts = {}
    for node in dag.get("nodes", {}).values():
        kind = node.get("kind", "Unknown")
        counts[kind] = counts.get(kind, 0) + 1
    if focus_atom_id:
        focus = neighborhood(dag, focus_atom_id, depth=1, direction="both")
        recent_nodes = focus["nodes"]
        heading = f"Neighborhood Around {focus_atom_id}"
    else:
        recent_nodes = [dag["nodes"][node_id] for node_id in dag.get("order", [])[-max_items:] if node_id in dag["nodes"]]
        heading = "Recent Nodes"
    frontier = frontier_nodes(dag, kind_filter=["Concluding", "Finding", "Deciding", "Action"])[:max_items]
    lines = [
        f"# {dag.get('title', dag.get('dag_id'))}",
        "",
        f"- dag_id: {dag.get('dag_id')}",
        f"- project: {dag.get('project_name', 'global')}",
        f"- objective: {dag.get('objective', '')}",
        f"- nodes: {len(dag.get('nodes', {}))}",
        f"- edges: {len(dag.get('edges', []))}",
        f"- counts: {counts}",
        "",
        "## Frontier",
    ]
    for node in frontier:
        lines.append(f"- {node.get('id')} {node.get('kind')}: {node.get('summary')}")
    lines.extend(["", f"## {heading}"])
    for node in recent_nodes:
        lines.append(f"- {node.get('id')} {node.get('kind')}: {node.get('summary')}")
    return "\n".join(lines).strip()


def tool_dag_summary(args):
    dag = load_dag(dag_id_arg(args), args.get("project_path"))
    max_items = int(args.get("max_items", 8))
    text = build_summary(dag, max_items, args.get("focus_atom_id"))
    return content_result(text, {"dag": dag_metadata(dag), "frontier": frontier_nodes(dag)[:max_items]})


def tool_dag_read(args):
    dag = load_dag(dag_id_arg(args), args.get("project_path"))
    include_nodes = bool(args.get("include_nodes", False))
    include_edges = bool(args.get("include_edges", True))
    limit = int(args.get("limit", 50))
    structured = {"dag": dag_metadata(dag)}
    if include_nodes:
        node_ids = dag.get("order", [])[-limit:]
        structured["nodes"] = [dag["nodes"][node_id] for node_id in node_ids if node_id in dag["nodes"]]
    if include_edges:
        structured["edges"] = dag.get("edges", [])[-limit:]
    return content_result(json.dumps(structured, indent=2, sort_keys=True), structured)


def tool_dag_neighbors(args):
    dag = load_dag(dag_id_arg(args), args.get("project_path"))
    structured = neighborhood(
        dag,
        require_str(args, "atom_id"),
        int(args.get("depth", 1)),
        args.get("direction", "both"),
    )
    return content_result(json.dumps(structured, indent=2, sort_keys=True), structured)


def tool_dag_frontier(args):
    dag = load_dag(dag_id_arg(args), args.get("project_path"))
    kinds = optional_list(args, "kinds")
    nodes = frontier_nodes(dag, kind_filter=kinds)[: int(args.get("limit", 20))]
    structured = {"dag_id": dag["dag_id"], "frontier": nodes}
    return content_result(json.dumps(structured, indent=2, sort_keys=True), structured)


def tool_dag_search(args):
    query = require_str(args, "query").lower()
    limit = int(args.get("limit", 20))
    matches = []
    for dag in all_dags(args.get("project_path")):
        if query in json.dumps(dag_metadata(dag)).lower():
            matches.append({"dag_id": dag["dag_id"], "title": dag.get("title"), "match": "dag metadata"})
        for node_id in dag.get("order", []):
            node = dag["nodes"].get(node_id, {})
            if query in json.dumps(node).lower():
                matches.append({"dag_id": dag["dag_id"], "atom_id": node_id, "kind": node.get("kind"), "summary": node.get("summary")})
                if len(matches) >= limit:
                    break
        if len(matches) >= limit:
            break
    structured = {"query": query, "matches": matches[:limit]}
    return content_result(json.dumps(structured, indent=2, sort_keys=True), structured)


def tool_dag_compact(args):
    dag = load_dag(dag_id_arg(args), args.get("project_path"))
    text = build_summary(dag, int(args.get("max_items", 12)), args.get("focus_atom_id"))
    conn = connect()
    try:
        conn.execute(
            """
            INSERT INTO summaries (dag_id, markdown, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(dag_id) DO UPDATE SET
              markdown = excluded.markdown,
              updated_at = excluded.updated_at
            """,
            (dag["dag_id"], text, now()),
        )
        conn.commit()
    finally:
        conn.close()
    structured = {"dag_id": dag["dag_id"], "database_path": str(database_path()), "summary": text}
    return content_result(text, structured)


def tool_dag_export(args):
    dag = load_dag(dag_id_arg(args), args.get("project_path"))
    export_format = args.get("format", "markdown")
    if export_format == "json":
        text = json.dumps(dag, indent=2, sort_keys=True)
    elif export_format == "xml":
        root = ET.Element("Scholia")
        step = ET.SubElement(root, "Step", {"id": "Step_export_01", "name": dag.get("title", "Exported DAG")})
        nodes = dag.get("nodes", {})
        child_parent = {}
        reference_targets = {}
        for edge in dag.get("edges", []):
            source = nodes.get(edge["from"], {})
            target = nodes.get(edge["to"], {})
            if (
                edge.get("relation") == "records_result"
                and source.get("kind") in {"Finding", "Concluding"}
                and target.get("kind") in {"Action", "Deciding"}
            ):
                child_parent[source["id"]] = target["id"]
            else:
                reference_targets.setdefault(source.get("id"), []).append(target.get("id"))

        goal_ids = [node_id for node_id in dag.get("order", []) if nodes[node_id].get("kind") == "Goal"]

        def node_element(node_id):
            node = nodes[node_id]
            kind = node["kind"]
            attrs = {"id": node_id}
            for key, value in (node.get("attributes") or {}).items():
                attrs[key] = ",".join(value) if isinstance(value, list) else str(value)
            if kind == "Concluding" and not attrs.get("for_goal") and len(goal_ids) == 1:
                attrs["for_goal"] = goal_ids[0]
            elem = ET.Element(kind, attrs)
            content = node.get("content") or node.get("summary") or ""
            refs = [target for target in reference_targets.get(node_id, []) if target]
            if kind == "Concluding" and attrs.get("for_goal") and "REFER:" not in content:
                refs.append(attrs["for_goal"])
            for target in dict.fromkeys(refs):
                marker = f"REFER:{target}"
                if marker not in content:
                    content = f"{content} {marker}".strip()
            elem.text = content
            for child_id, parent_id in child_parent.items():
                if parent_id == node_id:
                    elem.append(node_element(child_id))
            return elem

        for node_id in dag.get("order", []):
            if node_id not in child_parent:
                step.append(node_element(node_id))
        text = ET.tostring(root, encoding="unicode")
    else:
        lines = [build_summary(dag, 12), "", "## Nodes"]
        for node_id in dag.get("order", []):
            node = dag["nodes"][node_id]
            lines.append(f"### {node_id} {node.get('kind')}")
            lines.append(node.get("summary", ""))
            if node.get("content"):
                lines.extend(["", node["content"]])
            lines.append("")
        lines.append("## Edges")
        for edge in dag.get("edges", []):
            lines.append(f"- {edge['from']} -[{edge['relation']}]-> {edge['to']}")
        text = "\n".join(lines).strip()
    # Truncating machine-readable formats creates invalid JSON/XML. Their
    # integrity takes precedence over the display-oriented size hint.
    rendered = text if export_format in {"json", "xml"} else compact_text(text, int(args.get("max_chars", 20000)))
    return content_result(rendered, {"dag_id": dag["dag_id"], "format": export_format})


def tool_catalog(_args):
    structured = {
        "atoms": ATOMS,
        "operators": OPERATORS,
        "relations": RELATIONS,
        "resources": list(RESOURCE_TEXT),
        "database_path": str(database_path()),
        "scholia_atom_kinds_v05": sorted(getattr(SCHOLIA_ATOMS, "ATOM_KINDS", [])),
        "scholia_canonical_operators_v05": sorted(getattr(SCHOLIA_ATOMS, "CANONICAL_OPERATORS", [])),
        "scholia_criticality_rank": getattr(SCHOLIA_ATOMS, "CRITICALITY_RANK", {}),
        # Back-compat aliases retained for existing clients.
        "scholia_atom_kinds_v04": sorted(getattr(SCHOLIA_ATOMS, "ATOM_KINDS", [])),
        "scholia_canonical_operators_v04": sorted(getattr(SCHOLIA_ATOMS, "CANONICAL_OPERATORS", [])),
        "scholia_v031_edge_types": sorted(getattr(SCHOLIA_ATOMS, "V031_EDGE_TYPES", [])),
        "scholia_v04b_edge_types": sorted(getattr(SCHOLIA_ATOMS, "V04B_EDGE_TYPES", [])),
        "scholia_v031_effect_kinds": sorted(getattr(SCHOLIA_ATOMS, "V031_EFFECT_KINDS", [])),
        "scholia_v031_ref_types": sorted(getattr(SCHOLIA_ATOMS, "V031_REF_TYPES", [])),
        "scholia_validator_version": getattr(SCHOLIA_ATOMS, "SCHOLIA_VALIDATOR_VERSION", "unknown"),
        "lint_engine": LINT_ENGINE,
        "autoemit_default": True,
        "package_version": SERVER_VERSION,
        "language_grammar_version": "0.6.2",
    }
    return content_result(json.dumps(structured, indent=2, sort_keys=True), structured)


def tool_lookup(args):
    term = require_str(args, "term").lower()
    matches = []
    for item in ATOMS:
        if term in json.dumps(item).lower():
            matches.append({"type": "atom", **item})
    for item in OPERATORS:
        if term in json.dumps(item).lower():
            matches.append({"type": "operator", **item})
    for relation in RELATIONS:
        if term in relation:
            matches.append({"type": "relation", "name": relation})
    structured = {"term": term, "matches": matches}
    return content_result(json.dumps(structured, indent=2, sort_keys=True), structured, not matches)


def _lint_well_formed_tags(snippet):
    """Legacy lightweight check: XML-like tag matching only.

    Useful when a caller wants quick syntactic feedback without committing
    to the full Scholia grammar (e.g. inline snippet review). Returns the
    same shape as the original lint_snippet implementation.
    """
    errors = []
    stack = []
    for match in re.finditer(r"<(/?)([A-Za-z][A-Za-z0-9_-]*)([^<>]*?)(/?)>", snippet):
        closing, tag, attrs, self_closing = match.groups()
        if self_closing or attrs.strip().endswith("/"):
            continue
        if closing:
            if not stack:
                errors.append({"rule": "tag_balance", "atom_id": "", "message": f"closing tag without opener: {tag}"})
            else:
                opened = stack.pop()
                if opened != tag:
                    errors.append({"rule": "tag_balance", "atom_id": "", "message": f"tag mismatch: opened {opened}, closed {tag}"})
        else:
            stack.append(tag)
    for tag in reversed(stack):
        errors.append({"rule": "tag_balance", "atom_id": "", "message": f"unclosed tag: {tag}"})
    return errors


def _run_full_validator(snippet):
    """Parse + validate via the scholialang grammar.

    Returns a (ok, errors, warnings, parse_error, validator_version) tuple. On
    parse failure ``errors`` and ``warnings`` are empty and ``parse_error``
    carries the parser's diagnostic; callers should surface that as a single
    well-formedness violation so the lint output is uniform.
    """
    try:
        trace = SCHOLIA_PARSER.parse(snippet)
    except Exception as exc:  # parser raises on malformed input
        return (
            False,
            [],
            [],
            str(exc),
            getattr(SCHOLIA_ATOMS, "SCHOLIA_VALIDATOR_VERSION", "unknown"),
        )
    if not trace or not any(getattr(step, "atoms", ()) for step in trace):
        return (
            False,
            [],
            [],
            "Scholia input must contain at least one recognized atom.",
            getattr(SCHOLIA_ATOMS, "SCHOLIA_VALIDATOR_VERSION", "unknown"),
        )
    result = SCHOLIA_VALIDATOR.validate(trace)
    errors = [
        {
            "rule": err.rule,
            "atom_id": err.atom_id,
            "message": err.message,
            "severity": "error",
        }
        for err in result.errors
    ]
    warnings = [
        {
            "rule": warning.rule,
            "atom_id": warning.atom_id,
            "message": warning.message,
            "severity": "warning",
        }
        for warning in getattr(result, "warnings", [])
    ]
    return (
        result.ok,
        errors,
        warnings,
        None,
        getattr(result, "scholia_validator_version", getattr(SCHOLIA_ATOMS, "SCHOLIA_VALIDATOR_VERSION", "unknown")),
    )


def tool_lint_snippet(args):
    """Run the full Scholia grammar over a snippet.

    Back-compat: prior versions of this tool ran tag-balance checks only.
    The full validator is a strict superset — every malformed-tag case
    surfaces under rule ``well_formed`` (via the parser raise).
    Pass ``mode='tag_balance'`` to opt back into the legacy behaviour.
    """
    snippet = require_str(args, "snippet")
    mode = args.get("mode", "full")
    if mode == "tag_balance":
        errors = _lint_well_formed_tags(snippet)
        result = {
            "ok": not errors,
            "mode": "tag_balance",
            "errors": errors,
            "lint_engine": "tag-balance-only",
        }
        return content_result(json.dumps(result, indent=2), result, bool(errors))
    ok, errors, warnings, parse_error, validator_version = _run_full_validator(snippet)
    if parse_error is not None:
        errors = [
            {
                "rule": "well_formed",
                "atom_id": "",
                "message": parse_error,
                "severity": "error",
            }
        ]
        warnings = []
    result = {
        "ok": ok and not errors,
        "mode": "full",
        "errors": errors,
        "warnings": warnings,
        "lint_engine": LINT_ENGINE,
        "validator_version": validator_version,
    }
    return content_result(json.dumps(result, indent=2), result, not result["ok"])


def tool_lint_trace(args):
    """Structured per-rule validator output over a trace snippet.

    Same parse + validate path as ``lint_snippet`` (mode='full') but
    returns a per-rule breakdown so callers can drive UI rendering,
    CI gate logic, or counter-by-rule analytics. The vocabulary surfaced
    in ``rules`` matches scholialang's ``RULE_NAMES`` so consumers can
    branch on stable identifiers.
    """
    snippet = require_str(args, "snippet")
    ok, errors, warnings, parse_error, validator_version = _run_full_validator(snippet)
    if parse_error is not None:
        errors = [
            {
                "rule": "well_formed",
                "atom_id": "",
                "message": parse_error,
                "severity": "error",
            }
        ]
        warnings = []
        ok = False
    by_rule = {}
    for err in errors:
        by_rule.setdefault(err["rule"], []).append(err)
    warnings_by_rule = {}
    for warning in warnings:
        warnings_by_rule.setdefault(warning["rule"], []).append(warning)
    rule_names = tuple(getattr(SCHOLIA_VALIDATOR, "RULE_NAMES", ()))
    summary_counts = {rule: len(by_rule.get(rule, [])) for rule in rule_names}
    warning_counts = {rule: len(warnings_by_rule.get(rule, [])) for rule in rule_names}
    result = {
        "ok": ok and not errors,
        "total_errors": len(errors),
        "total_warnings": len(warnings),
        "errors": errors,
        "warnings": warnings,
        "errors_by_rule": by_rule,
        "warnings_by_rule": warnings_by_rule,
        "counts_by_rule": summary_counts,
        "warning_counts_by_rule": warning_counts,
        "rules": list(rule_names),
        "lint_engine": LINT_ENGINE,
        "validator_version": validator_version,
    }
    return content_result(json.dumps(result, indent=2, sort_keys=True), result, not result["ok"])


SENSITIVE_TEXT_RE = re.compile(
    r"(?i)(authorization|bearer\s+[A-Za-z0-9._~+/=-]+|api[_-]?key|secret|password|private[_ -]?key|access[_-]?token|refresh[_-]?token)"
)


def codex_home(args):
    return Path(args.get("codex_home") or os.environ.get("CODEX_HOME", "~/.codex")).expanduser()


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()


def text_record(label, value, include_text=True, max_chars=1200):
    text = "" if value is None else str(value)
    record = {
        "label": label,
        "length": len(text),
        "sha256": sha256_text(text),
    }
    if not text:
        record["text"] = ""
        return record
    if SENSITIVE_TEXT_RE.search(text):
        record["text_omitted_reason"] = "sensitive-looking content"
        return record
    if not include_text:
        record["text_omitted_reason"] = "configured for hash/reference only"
        return record
    record["text"] = compact_text(text, max_chars)
    record["truncated"] = len(text) > max_chars
    return record


def json_text_record(label, value, include_text=True, max_chars=1200):
    return text_record(label, json.dumps(value, sort_keys=True, separators=(",", ":")), include_text, max_chars)


def parse_jsonish(value):
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except Exception:
        return value


def load_codex_thread_row(home, thread_id=None, project_path=None):
    state_path = home / "state_5.sqlite"
    if not state_path.exists():
        return None
    conn = sqlite3.connect(str(state_path))
    conn.row_factory = sqlite3.Row
    try:
        if thread_id:
            return conn.execute("SELECT * FROM threads WHERE id = ?", (thread_id,)).fetchone()
        if project_path:
            cwd = str(Path(project_path).expanduser().resolve(strict=False))
            return conn.execute(
                """
                SELECT * FROM threads
                WHERE cwd = ? AND archived = 0
                ORDER BY updated_at_ms DESC, updated_at DESC
                LIMIT 1
                """,
                (cwd,),
            ).fetchone()
        return conn.execute(
            "SELECT * FROM threads WHERE archived = 0 ORDER BY updated_at_ms DESC, updated_at DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()


def load_codex_thread_row_by_rollout_path(home, rollout_path):
    state_path = home / "state_5.sqlite"
    if not state_path.exists() or not rollout_path:
        return None
    path = Path(rollout_path).expanduser()
    candidates = list(dict.fromkeys([str(rollout_path), str(path), str(path.resolve(strict=False))]))
    placeholders = ",".join("?" for _ in candidates)
    conn = sqlite3.connect(str(state_path))
    conn.row_factory = sqlite3.Row
    try:
        return conn.execute(
            f"""
            SELECT * FROM threads
            WHERE rollout_path IN ({placeholders})
            ORDER BY archived ASC, updated_at_ms DESC, updated_at DESC
            LIMIT 1
            """,
            tuple(candidates),
        ).fetchone()
    finally:
        conn.close()


def codex_event_atom_kind(payload_type, payload):
    if payload_type in {"function_call", "tool_search_call", "custom_tool_call", "patch_apply_begin"}:
        return "Action"
    if payload_type in {"function_call_output", "tool_search_output", "custom_tool_call_output", "patch_apply_end", "token_count", "reasoning", "task_started"}:
        return "Observation"
    if payload_type == "user_message":
        return "Question"
    if payload_type == "agent_message":
        return "Finding"
    if payload_type == "task_complete":
        return "Concluding"
    if payload_type == "message":
        role = payload.get("role")
        if role == "user":
            return "Question"
        if role == "assistant":
            return "Finding"
    return "Observation"


def codex_event_summary(index, top_type, payload_type, payload):
    prefix = f"Codex rollout event {index:04d}: {top_type}/{payload_type}"
    if payload_type in {"function_call", "tool_search_call", "custom_tool_call"}:
        return compact_text(f"{prefix} calls {payload.get('name') or 'tool_search'}", 500)
    if payload_type in {"function_call_output", "tool_search_output", "custom_tool_call_output"}:
        return compact_text(f"{prefix} returns output for {payload.get('call_id', 'unknown call')}", 500)
    if payload_type == "patch_apply_end":
        return compact_text(f"{prefix} completed apply_patch for {payload.get('call_id', 'unknown call')}", 500)
    if payload_type == "message":
        return compact_text(f"{prefix} role={payload.get('role', 'unknown')}", 500)
    if payload_type == "agent_message":
        phase = payload.get("phase")
        return compact_text(f"{prefix} phase={phase or 'unknown'}", 500)
    if payload_type == "user_message":
        return compact_text(f"{prefix} captures user prompt", 500)
    if payload_type == "reasoning":
        return compact_text(f"{prefix} records non-materialized reasoning metadata", 500)
    return compact_text(prefix, 500)


def codex_event_content(index, line_no, raw_line, obj, args):
    max_chars = int(args.get("max_content_chars", 1200))
    include_tool_text = bool(args.get("include_tool_text", True))
    include_agent_text = bool(args.get("include_agent_text", True))
    include_user_text = bool(args.get("include_user_text", True))
    include_instruction_text = bool(args.get("include_instruction_text", False))

    payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
    payload_type = payload.get("type", obj.get("type", "unknown"))
    top_type = obj.get("type", "unknown")
    role = payload.get("role")
    records = {
        "event_index": index,
        "line": line_no,
        "timestamp": obj.get("timestamp"),
        "top_type": top_type,
        "payload_type": payload_type,
        "raw_line_sha256": sha256_text(raw_line),
    }
    for key in ("turn_id", "call_id", "name", "status", "execution", "phase"):
        if key in payload:
            records[key] = payload.get(key)
    if role:
        records["role"] = role

    extracted = {}
    if payload_type == "user_message":
        extracted["message"] = text_record("user_message", payload.get("message", ""), include_user_text, max_chars)
    elif payload_type == "agent_message":
        extracted["message"] = text_record("agent_message", payload.get("message", ""), include_agent_text, max_chars)
    elif payload_type == "task_complete":
        extracted["last_agent_message"] = text_record(
            "last_agent_message", payload.get("last_agent_message", ""), include_agent_text, max_chars
        )
        for key in ("completed_at", "duration_ms", "time_to_first_token_ms"):
            if key in payload:
                records[key] = payload.get(key)
    elif payload_type in {"function_call", "function_call_output", "custom_tool_call", "custom_tool_call_output"}:
        key = "arguments" if payload_type == "function_call" else "input" if payload_type == "custom_tool_call" else "output"
        extracted[key] = text_record(key, payload.get(key, ""), include_tool_text, max_chars)
    elif payload_type == "patch_apply_end":
        extracted["stdout"] = text_record("stdout", payload.get("stdout", ""), include_tool_text, max_chars)
        extracted["stderr"] = text_record("stderr", payload.get("stderr", ""), include_tool_text, max_chars)
        extracted["changes"] = json_text_record("changes", payload.get("changes", {}), include_tool_text, max_chars)
    elif payload_type == "tool_search_call":
        extracted["arguments"] = json_text_record("arguments", payload.get("arguments", {}), include_tool_text, max_chars)
    elif payload_type == "tool_search_output":
        extracted["tools"] = json_text_record("tools", payload.get("tools", []), include_tool_text, max_chars)
    elif payload_type == "message":
        parts = []
        include_text = include_user_text if role == "user" else include_instruction_text if role in {"system", "developer"} else include_agent_text
        for offset, item in enumerate(payload.get("content", []) or []):
            if isinstance(item, dict) and "text" in item:
                parts.append(text_record(f"message.content[{offset}].text", item.get("text", ""), include_text, max_chars))
            else:
                parts.append(json_text_record(f"message.content[{offset}]", item, False, max_chars))
        extracted["content"] = parts
    elif payload_type == "reasoning":
        # Hidden reasoning is intentionally not materialized. Preserve evidence
        # that it existed without copying encrypted or private chain-of-thought.
        if payload.get("encrypted_content"):
            extracted["encrypted_content"] = text_record("encrypted_content", payload.get("encrypted_content", ""), False, max_chars)
        if payload.get("summary"):
            extracted["summary"] = json_text_record("summary", payload.get("summary"), include_agent_text, max_chars)
    elif payload_type == "token_count":
        extracted["info"] = payload.get("info", {})
        extracted["rate_limits"] = payload.get("rate_limits", {})
    elif obj.get("type") in {"item.completed", "turn.completed", "thread.started", "turn.started"}:
        extracted["top_level_event"] = json_text_record("top_level_event", obj, include_tool_text, max_chars)
    else:
        extracted["payload"] = json_text_record("payload", payload, False, max_chars)

    return json.dumps({"metadata": records, "extracted": extracted}, indent=2, sort_keys=True)


def codex_compact_detail(value, label, include_text=True, max_chars=1200):
    if isinstance(value, str):
        if not value:
            return ""
        if SENSITIVE_TEXT_RE.search(value) or not include_text or len(value) > max_chars:
            return text_record(label, value, include_text, max_chars)
        return value
    if isinstance(value, list):
        return [codex_compact_detail(item, f"{label}[{index}]", include_text, max_chars) for index, item in enumerate(value)]
    if isinstance(value, dict):
        compacted = {}
        for key, item in value.items():
            key_label = str(key)
            compacted[key] = codex_compact_detail(item, key_label, include_text, max_chars)
        return compacted
    return value


def codex_text_detail(label, value, include_text, args):
    return codex_compact_detail(value or "", label, include_text, int(args.get("max_content_chars", 1200)))


def codex_raw_output_details(raw_line, line_no, source, args, include_text=False):
    return {
        "stream": "stdout",
        "line": codex_text_detail("line", raw_line, include_text, args),
        "source": source,
        "rollout_line": line_no,
        "raw_line_sha256": sha256_text(raw_line),
    }


def codex_event_record(event_type, run_id, task_id, details, timestamp=None, status="running"):
    return {
        "event": event_type,
        "run_id": run_id,
        "task_id": task_id,
        "status": status,
        "details": details,
        "timestamp": timestamp,
    }


def codex_message_content_details(payload, args):
    role = payload.get("role")
    include_user_text = bool(args.get("include_user_text", True))
    include_agent_text = bool(args.get("include_agent_text", True))
    include_instruction_text = bool(args.get("include_instruction_text", False))
    include_text = include_user_text if role == "user" else include_instruction_text if role in {"system", "developer"} else include_agent_text
    records = []
    for offset, item in enumerate(payload.get("content", []) or []):
        if isinstance(item, dict) and "text" in item:
            records.append(codex_text_detail(f"content[{offset}].text", item.get("text", ""), include_text, args))
        else:
            records.append(json_text_record(f"content[{offset}]", item, False, int(args.get("max_content_chars", 1200))))
    return records


def codex_cli_command_output(item):
    raw_output = item.get("aggregated_output")
    if isinstance(raw_output, str):
        return raw_output
    stdout = item.get("stdout") if isinstance(item.get("stdout"), str) else ""
    stderr = item.get("stderr") if isinstance(item.get("stderr"), str) else ""
    if stdout and stderr:
        return f"{stdout}\n{stderr}"
    return stdout or stderr or ""


def codex_cli_canonical_events(obj, raw_line, line_no, run_id, task_id, args):
    timestamp = obj.get("timestamp")
    include_tool_text = bool(args.get("include_tool_text", True))
    include_agent_text = bool(args.get("include_agent_text", True))
    top_type = obj.get("type")

    if top_type == "item.completed":
        raw_item = obj.get("item")
        item = raw_item if isinstance(raw_item, dict) else {}
        item_type = item.get("type")
        if item_type == "agent_message":
            details = {
                "text": codex_text_detail("text", item.get("text", ""), include_agent_text, args),
                "role": "assistant",
            }
            return [codex_event_record("task_message", run_id, task_id, details, timestamp)]
        if item_type in {"tool_use", "tool_call"}:
            details = {
                "tool": item.get("name"),
                "id": item.get("id"),
                "input": codex_compact_detail(item.get("input", {}), "input", include_tool_text, int(args.get("max_content_chars", 1200))),
            }
            return [codex_event_record("task_tool_call", run_id, task_id, details, timestamp)]
        if item_type in {"tool_result", "tool_output"}:
            details = {
                "tool_use_id": item.get("tool_use_id") or item.get("id"),
                "content": codex_text_detail("content", item.get("output") or item.get("content") or "", include_tool_text, args),
                "is_error": bool(item.get("is_error", False)),
            }
            return [codex_event_record("task_tool_result", run_id, task_id, details, timestamp)]
        if item_type == "command_execution":
            cmd = item.get("command") if isinstance(item.get("command"), str) else ""
            output = codex_cli_command_output(item)
            item_id = item.get("id")
            exit_code = item.get("exit_code")
            is_error = isinstance(exit_code, int) and exit_code != 0
            return [
                codex_event_record(
                    "task_tool_call",
                    run_id,
                    task_id,
                    {
                        "tool": "bash",
                        "id": item_id,
                        "input": {"command": codex_text_detail("command", cmd, include_tool_text, args)},
                    },
                    timestamp,
                ),
                codex_event_record(
                    "task_tool_result",
                    run_id,
                    task_id,
                    {
                        "tool_use_id": item_id,
                        "content": codex_text_detail("content", output, include_tool_text, args),
                        "is_error": is_error,
                    },
                    timestamp,
                ),
                codex_event_record(
                    "task_output",
                    run_id,
                    task_id,
                    codex_raw_output_details(raw_line, line_no, "codex_command_execution", args, include_text=False),
                    timestamp,
                ),
            ]
        return [
            codex_event_record(
                "task_output",
                run_id,
                task_id,
                codex_raw_output_details(raw_line, line_no, f"codex_item_completed:{item_type or 'unknown'}", args, include_text=False),
                timestamp,
            )
        ]

    if top_type == "turn.completed":
        usage = obj.get("usage") if isinstance(obj.get("usage"), dict) else {}
        if not usage:
            return []
        details = {
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "cache_read_input_tokens": usage.get("cached_input_tokens", 0),
            "model": os.environ.get("CODEX_MODEL", "codex") or "codex",
        }
        return [codex_event_record("token_usage", run_id, task_id, details, timestamp)]

    return [
        codex_event_record(
            "task_output",
            run_id,
            task_id,
            codex_raw_output_details(raw_line, line_no, top_type or "codex_lifecycle", args, include_text=False),
            timestamp,
        )
    ]


def codex_desktop_canonical_events(obj, raw_line, line_no, run_id, task_id, args):
    payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
    payload_type = payload.get("type", obj.get("type", "unknown"))
    timestamp = obj.get("timestamp")
    include_user_text = bool(args.get("include_user_text", True))
    include_agent_text = bool(args.get("include_agent_text", True))
    include_tool_text = bool(args.get("include_tool_text", True))

    if payload_type == "user_message":
        details = {
            "text": codex_text_detail("text", payload.get("message", ""), include_user_text, args),
            "role": "user",
        }
        return [codex_event_record("task_message", run_id, task_id, details, timestamp)]

    if payload_type == "agent_message":
        details = {
            "text": codex_text_detail("text", payload.get("message", ""), include_agent_text, args),
            "role": "assistant",
        }
        if payload.get("phase"):
            details["phase"] = payload.get("phase")
        return [codex_event_record("task_message", run_id, task_id, details, timestamp)]

    if payload_type == "message":
        role = payload.get("role", "unknown")
        details = {"role": role, "content": codex_message_content_details(payload, args)}
        if payload.get("phase"):
            details["phase"] = payload.get("phase")
        event_type = "task_message" if role in {"user", "assistant"} else "task_output"
        return [codex_event_record(event_type, run_id, task_id, details, timestamp)]

    if payload_type in {"function_call", "tool_search_call", "custom_tool_call"}:
        raw_input = payload.get("arguments") if payload_type in {"function_call", "tool_search_call"} else payload.get("input", "")
        parsed_input = parse_jsonish(raw_input)
        if isinstance(parsed_input, str):
            normalized_input = codex_text_detail("input", parsed_input, include_tool_text, args)
        else:
            normalized_input = codex_compact_detail(parsed_input, "input", include_tool_text, int(args.get("max_content_chars", 1200)))
        details = {
            "tool": payload.get("name") or ("tool_search" if payload_type == "tool_search_call" else "tool"),
            "id": payload.get("call_id"),
            "input": normalized_input,
        }
        return [codex_event_record("task_tool_call", run_id, task_id, details, timestamp)]

    if payload_type in {"function_call_output", "tool_search_output", "custom_tool_call_output"}:
        output = payload.get("output", payload.get("tools", ""))
        if payload_type == "tool_search_output":
            content = codex_compact_detail({"tools": payload.get("tools", [])}, "content", include_tool_text, int(args.get("max_content_chars", 1200)))
        else:
            content = codex_text_detail("content", output, include_tool_text, args)
        details = {
            "tool_use_id": payload.get("call_id"),
            "content": content,
            "is_error": bool(payload.get("is_error", False)),
        }
        return [codex_event_record("task_tool_result", run_id, task_id, details, timestamp)]

    if payload_type == "patch_apply_end":
        stderr = payload.get("stderr", "")
        details = {
            "tool_use_id": payload.get("call_id"),
            "content": {
                "stdout": codex_text_detail("stdout", payload.get("stdout", ""), include_tool_text, args),
                "stderr": codex_text_detail("stderr", stderr, include_tool_text, args),
                "changes": codex_compact_detail(payload.get("changes", {}), "changes", include_tool_text, int(args.get("max_content_chars", 1200))),
            },
            "is_error": bool(payload.get("is_error", False) or stderr),
        }
        return [codex_event_record("task_tool_result", run_id, task_id, details, timestamp)]

    if payload_type == "token_count":
        info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
        details = {
            "input_tokens": info.get("input_tokens", info.get("total_token_count", 0)),
            "output_tokens": info.get("output_tokens", 0),
            "cache_read_input_tokens": info.get("cached_input_tokens", 0),
            "model": info.get("model") or obj.get("model") or os.environ.get("CODEX_MODEL", "codex") or "codex",
            "info": info,
            "rate_limits": payload.get("rate_limits", {}),
        }
        return [codex_event_record("token_usage", run_id, task_id, details, timestamp)]

    if payload_type == "task_complete":
        details = {
            "last_agent_message": codex_text_detail("last_agent_message", payload.get("last_agent_message", ""), include_agent_text, args),
            "completed_at": payload.get("completed_at"),
            "duration_ms": payload.get("duration_ms"),
            "time_to_first_token_ms": payload.get("time_to_first_token_ms"),
        }
        return [codex_event_record("task_output", run_id, task_id, details, timestamp, status="completed")]

    if payload_type == "reasoning":
        details = {"source": "codex_reasoning_metadata"}
        if payload.get("encrypted_content"):
            details["encrypted_content"] = codex_text_detail("encrypted_content", payload.get("encrypted_content", ""), False, args)
        if payload.get("summary"):
            details["summary"] = json_text_record("summary", payload.get("summary"), include_agent_text, int(args.get("max_content_chars", 1200)))
        return [codex_event_record("task_output", run_id, task_id, details, timestamp)]

    return [
        codex_event_record(
            "task_output",
            run_id,
            task_id,
            codex_raw_output_details(raw_line, line_no, payload_type or obj.get("type", "codex_desktop_event"), args, include_text=False),
            timestamp,
        )
    ]


def codex_canonical_events(obj, raw_line, line_no, run_id, task_id, args):
    if obj.get("type") in {"item.completed", "turn.completed", "thread.started", "turn.started"}:
        return codex_cli_canonical_events(obj, raw_line, line_no, run_id, task_id, args)
    return codex_desktop_canonical_events(obj, raw_line, line_no, run_id, task_id, args)


def codex_canonical_atom_kind(event_type, details):
    if event_type == "task_tool_call":
        return "Action"
    if event_type in {"task_tool_result", "task_output", "token_usage"}:
        return "Observation"
    if event_type == "task_message":
        if isinstance(details, dict) and details.get("role") == "user":
            return "Question"
        return "Finding"
    return "Observation"


def codex_canonical_summary(raw_index, canonical_index, record):
    event_type = record.get("event", "unknown")
    details = record.get("details") if isinstance(record.get("details"), dict) else {}
    suffix = ""
    if event_type == "task_tool_call":
        suffix = f" calls {details.get('tool') or 'tool'}"
    elif event_type == "task_tool_result":
        suffix = f" returns output for {details.get('tool_use_id') or 'unknown tool'}"
    elif event_type == "task_message":
        suffix = f" role={details.get('role', 'unknown')}"
    return compact_text(f"Codex canonical event {raw_index:04d}.{canonical_index}: {event_type}{suffix}", 500)


def codex_canonical_content(record, raw_line, line_no):
    content = {
        **record,
        "scholia": {
            "source": "internal agent harness:rsi_codex_parser",
            "rollout_line": line_no,
            "raw_line_sha256": sha256_text(raw_line),
        },
    }
    return json.dumps(content, indent=2, sort_keys=True)


def tool_codex_import_thread(args):
    home = codex_home(args)
    project_path = args.get("project_path")
    thread_id = args.get("thread_id")
    rollout_path = args.get("rollout_path")
    if rollout_path and not thread_id:
        thread_row = load_codex_thread_row_by_rollout_path(home, rollout_path)
    else:
        thread_row = load_codex_thread_row(home, thread_id, project_path)
    if thread_row is not None:
        thread_id = thread_row["id"]

    if not rollout_path and thread_row is not None:
        rollout_path = thread_row["rollout_path"]
    if not rollout_path:
        raise ValueError("rollout_path or a resolvable thread_id/project_path is required")

    path = Path(rollout_path).expanduser()
    if not path.exists():
        raise ValueError(f"rollout file not found: {path}")

    max_events = int(args.get("max_events", 2000))
    if max_events <= 0:
        raise ValueError("max_events must be positive")

    raw_text = path.read_text(errors="replace")
    raw_lines = raw_text.splitlines()
    title = args.get("title")
    if not title:
        if thread_row is not None and thread_row["title"]:
            title = f"Codex exhaust: {thread_row['title']}"
        else:
            title = f"Codex exhaust: {thread_id or path.stem}"

    dag_id = args.get("dag_id")
    if dag_id:
        existing_dag = load_dag(dag_id, project_path)
        goal_atom_id = next(
            (node_id for node_id in existing_dag.get("order", []) if existing_dag["nodes"].get(node_id, {}).get("kind") == "Goal"),
            None,
        )
    else:
        started = tool_dag_start(
            {
                "project_path": project_path,
                "title": title,
                "objective": "Preserve the observable Codex rollout exhaust as an event-sourced Scholialang DAG.",
                "tags": ["codex", "exhaust", "rollout", "event-source"],
            }
        )
        dag_id = started["structuredContent"]["dag_id"]
        goal_atom = started["structuredContent"].get("goal_atom") or {}
        goal_atom_id = goal_atom.get("id")

    imported = 0
    parse_errors = 0
    previous_atom_id = None
    call_atoms = {}
    canonical_call_atoms = {}
    event_atoms = []
    payload_counts = {}
    canonical_counts = {}
    canonical_imported = 0
    include_canonical_events = bool(args.get("include_canonical_events", True))
    run_id = args.get("run_id") or thread_id or path.stem
    current_task_id = args.get("task_id") or thread_id or path.stem

    metadata = {
        "thread_id": thread_id,
        "run_id": run_id,
        "initial_task_id": current_task_id,
        "rollout_path": str(path),
        "rollout_line_count": len(raw_lines),
        "rollout_sha256": sha256_text(raw_text),
        "codex_home": str(home),
        "canonical_events": include_canonical_events,
        "canonical_policy": "internal agent harness stage rsi_codex_parser parity: preserve raw rollout atoms and derive task_message/task_tool_call/task_tool_result/token_usage/task_output envelopes.",
    }
    if thread_row is not None:
        for key in ("title", "cwd", "model", "reasoning_effort", "source", "thread_source", "tokens_used"):
            if key in thread_row.keys():
                metadata[key] = thread_row[key]

    root_links = []
    if goal_atom_id:
        root_links.append({"to": goal_atom_id, "relation": "refers", "label": "rollout source for trace goal"})
    root_atom = tool_dag_add_atom(
        {
            "dag_id": dag_id,
            "project_path": project_path,
            "kind": "Observation",
            "summary": f"Codex rollout source resolved for {thread_id or path.name}.",
            "content": json.dumps(metadata, indent=2, sort_keys=True),
            "files": [str(path)],
            "links": root_links,
        }
    )["structuredContent"]["atom"]["id"]
    previous_atom_id = root_atom

    for line_no, raw_line in enumerate(raw_lines[:max_events], start=1):
        obj = None
        try:
            obj = json.loads(raw_line)
        except Exception as exc:
            parse_errors += 1
            summary = f"Codex rollout event {line_no:04d}: JSON parse error"
            content = json.dumps(
                {
                    "line": line_no,
                    "raw_line_sha256": sha256_text(raw_line),
                    "error": str(exc),
                },
                indent=2,
                sort_keys=True,
            )
            kind = "Contradiction"
            payload = {}
            payload_type = "parse_error"
        else:
            if not isinstance(obj, dict):
                payload = {}
                payload_type = f"json_{type(obj).__name__}"
                payload_counts[payload_type] = payload_counts.get(payload_type, 0) + 1
                kind = "Observation"
                summary = f"Codex rollout event {line_no:04d}: non-object JSON value"
                content = json.dumps(
                    {
                        "event_index": line_no,
                        "line": line_no,
                        "payload_type": payload_type,
                        "raw_line_sha256": sha256_text(raw_line),
                        "value": json_text_record("value", obj, False, int(args.get("max_content_chars", 1200))),
                    },
                    indent=2,
                    sort_keys=True,
                )
            else:
                payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
                payload_type = payload.get("type", obj.get("type", "unknown"))
                if isinstance(payload.get("turn_id"), str) and payload.get("turn_id"):
                    current_task_id = payload.get("turn_id")
                payload_counts[payload_type] = payload_counts.get(payload_type, 0) + 1
                kind = codex_event_atom_kind(payload_type, payload)
                summary = codex_event_summary(line_no, obj.get("type", "unknown"), payload_type, payload)
                content = codex_event_content(line_no, line_no, raw_line, obj, args)

        links = [{"to": previous_atom_id, "relation": "after"}] if previous_atom_id else [{"to": root_atom, "relation": "derived_from"}]
        call_id = payload.get("call_id") if isinstance(payload, dict) else None
        if payload_type in {"function_call_output", "tool_search_output", "custom_tool_call_output", "patch_apply_end"} and call_id in call_atoms:
            links.append({"to": call_atoms[call_id], "relation": "derived_from", "label": "tool output for call_id"})

        atom = tool_dag_add_atom(
            {
                "dag_id": dag_id,
                "project_path": project_path,
                "kind": kind,
                "summary": summary,
                "content": content,
                "files": [f"{path}:{line_no}"],
                "links": links,
            }
        )["structuredContent"]["atom"]
        atom_id = atom["id"]
        event_atoms.append(atom_id)
        if payload_type in {"function_call", "tool_search_call", "custom_tool_call"} and call_id:
            call_atoms[call_id] = atom_id
        previous_atom_id = atom_id
        imported += 1

        if include_canonical_events and isinstance(obj, dict):
            try:
                canonical_records = codex_canonical_events(obj, raw_line, line_no, run_id, current_task_id, args)
            except Exception as exc:
                canonical_records = [
                    codex_event_record(
                        "task_output",
                        run_id,
                        current_task_id,
                        {
                            "source": "codex_canonicalization_error",
                            "error": str(exc),
                            "rollout_line": line_no,
                            "raw_line_sha256": sha256_text(raw_line),
                        },
                        None,
                    )
                ]
            for canonical_index, record in enumerate(canonical_records, start=1):
                event_type = record.get("event", "task_output")
                details = record.get("details") if isinstance(record.get("details"), dict) else {}
                canonical_links = [
                    {
                        "to": atom_id,
                        "relation": "derived_from",
                        "label": "canonical internal agent harness event derived from raw Codex rollout event",
                    }
                ]
                if event_type == "task_tool_result":
                    tool_use_id = details.get("tool_use_id")
                    if tool_use_id in canonical_call_atoms:
                        canonical_links.append(
                            {
                                "to": canonical_call_atoms[tool_use_id],
                                "relation": "derived_from",
                                "label": "canonical tool result for tool_use_id",
                            }
                        )

                canonical_atom = tool_dag_add_atom(
                    {
                        "dag_id": dag_id,
                        "project_path": project_path,
                        "kind": codex_canonical_atom_kind(event_type, details),
                        "summary": codex_canonical_summary(line_no, canonical_index, record),
                        "content": codex_canonical_content(record, raw_line, line_no),
                        "files": [f"{path}:{line_no}"],
                        "links": canonical_links,
                    }
                )["structuredContent"]["atom"]
                event_atoms.append(canonical_atom["id"])
                canonical_imported += 1
                canonical_counts[event_type] = canonical_counts.get(event_type, 0) + 1
                if event_type == "task_tool_call" and details.get("id"):
                    canonical_call_atoms[details.get("id")] = canonical_atom["id"]

    if len(raw_lines) > max_events:
        summary = f"Codex rollout import stopped at max_events={max_events}; {len(raw_lines) - max_events} events remain referenced only."
        previous_atom_id = tool_dag_add_atom(
            {
                "dag_id": dag_id,
                "project_path": project_path,
                "kind": "Observation",
                "summary": summary,
                "content": json.dumps({"remaining_events": len(raw_lines) - max_events, "rollout_path": str(path)}, indent=2),
                "files": [str(path)],
                "links": [{"to": previous_atom_id, "relation": "after"}],
            }
        )["structuredContent"]["atom"]["id"]

    final_summary = {
        "events_imported": imported,
        "canonical_events_imported": canonical_imported,
        "parse_errors": parse_errors,
        "payload_counts": payload_counts,
        "canonical_counts": canonical_counts,
        "rollout_path": str(path),
        "raw_rollout_sha256": metadata["rollout_sha256"],
        "canonical_policy": metadata["canonical_policy"],
        "hidden_reasoning_policy": "reasoning/encrypted_content is recorded by length/hash/reference only, never materialized",
    }
    final_links = [{"to": previous_atom_id, "relation": "after"}]
    if goal_atom_id:
        final_links.append({"to": goal_atom_id, "relation": "derived_from", "label": "for_goal status=met"})
    tool_dag_add_atom(
        {
            "dag_id": dag_id,
            "project_path": project_path,
            "kind": "Concluding",
            "summary": f"Imported {imported} Codex rollout events into an observable exhaust trail.",
            "content": json.dumps(final_summary, indent=2, sort_keys=True),
            "files": [str(path)],
            "links": final_links,
        }
    )
    tool_dag_compact({"dag_id": dag_id, "project_path": project_path, "max_items": 20})

    structured = {
        "dag_id": dag_id,
        "trace_id": dag_id,
        "thread_id": thread_id,
        "rollout_path": str(path),
        "events_imported": imported,
        "canonical_events_imported": canonical_imported,
        "parse_errors": parse_errors,
        "payload_counts": payload_counts,
        "canonical_counts": canonical_counts,
        "database_path": str(database_path()),
    }
    return content_result(json.dumps(structured, indent=2, sort_keys=True), structured)


CANONICAL_TOOLS = {
    "scholia_dag_start": tool_dag_start,
    "scholia_dag_add_atom": tool_dag_add_atom,
    "scholia_dag_set_model": tool_dag_set_model,
    "scholia_dag_link": tool_dag_link,
    "scholia_dag_ensure_session": tool_dag_ensure_session,
    "scholia_dag_finish_session": tool_dag_finish_session,
    "scholia_dag_list": tool_dag_list,
    "scholia_dag_summary": tool_dag_summary,
    "scholia_dag_read": tool_dag_read,
    "scholia_dag_neighbors": tool_dag_neighbors,
    "scholia_dag_frontier": tool_dag_frontier,
    "scholia_dag_search": tool_dag_search,
    "scholia_dag_compact": tool_dag_compact,
    "scholia_dag_export": tool_dag_export,
    "scholia_codex_import_thread": tool_codex_import_thread,
    "scholia_trace_start": tool_dag_start,
    "scholia_trace_append": tool_dag_add_atom,
    "scholia_trace_list": tool_dag_list,
    "scholia_trace_summary": tool_dag_summary,
    "scholia_trace_read": tool_dag_read,
    "scholia_trace_search": tool_dag_search,
    "scholia_trace_compact": tool_dag_compact,
    "scholia_trace_export": tool_dag_export,
    "scholia_catalog": tool_catalog,
    "scholia_lookup": tool_lookup,
    "scholia_lint_snippet": tool_lint_snippet,
    "scholia_lint_trace": tool_lint_trace,
}

LEGACY_TOOL_ALIASES = {
    "scholia.dag_start": "scholia_dag_start",
    "scholia.dag_add_atom": "scholia_dag_add_atom",
    "scholia.dag_set_model": "scholia_dag_set_model",
    "scholia.dag_link": "scholia_dag_link",
    "scholia.dag_ensure_session": "scholia_dag_ensure_session",
    "scholia.dag_finish_session": "scholia_dag_finish_session",
    "scholia.dag_list": "scholia_dag_list",
    "scholia.dag_summary": "scholia_dag_summary",
    "scholia.dag_read": "scholia_dag_read",
    "scholia.dag_neighbors": "scholia_dag_neighbors",
    "scholia.dag_frontier": "scholia_dag_frontier",
    "scholia.dag_search": "scholia_dag_search",
    "scholia.dag_compact": "scholia_dag_compact",
    "scholia.dag_export": "scholia_dag_export",
    "scholia.codex_import_thread": "scholia_codex_import_thread",
    "scholia.trace_start": "scholia_trace_start",
    "scholia.trace_append": "scholia_trace_append",
    "scholia.trace_list": "scholia_trace_list",
    "scholia.trace_summary": "scholia_trace_summary",
    "scholia.trace_read": "scholia_trace_read",
    "scholia.trace_search": "scholia_trace_search",
    "scholia.trace_compact": "scholia_trace_compact",
    "scholia.trace_export": "scholia_trace_export",
    "scholia.catalog": "scholia_catalog",
    "scholia.lookup": "scholia_lookup",
    "scholia.lint_snippet": "scholia_lint_snippet",
    "scholia.lint_trace": "scholia_lint_trace",
}

TOOLS = {
    **CANONICAL_TOOLS,
    **{legacy: CANONICAL_TOOLS[canonical] for legacy, canonical in LEGACY_TOOL_ALIASES.items()},
}


def schema(properties=None, required=None):
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties or {},
        "required": required or [],
    }


def server_info():
    return {"name": SERVER_NAME, "title": "Scholialang", "version": SERVER_VERSION}


def modern_protocol_version(params):
    """Return the per-request modern protocol version, if declared."""
    meta = params.get("_meta")
    if isinstance(meta, dict) and meta.get(META_PROTOCOL_VERSION):
        return str(meta.get(META_PROTOCOL_VERSION))
    return None


def legacy_protocol_version(params):
    requested = params.get("protocolVersion")
    return str(requested) if requested else None


def tool_schema(name):
    common_dag = {"dag_id": {"type": "string"}, "trace_id": {"type": "string"}, "project_path": {"type": "string"}}
    if name.endswith("dag_set_model"):
        return schema({
            **common_dag,
            "model": {"type": "string", "description": "Model identifier that produced this DAG."},
            "overwrite": {"type": "boolean", "description": "Replace an already-recorded model instead of keeping the first."},
        }, ["model"])
    if name.endswith("dag_start") or name.endswith("trace_start"):
        return schema({
            "project_path": {"type": "string"},
            "title": {"type": "string"},
            "objective": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
            "model": {"type": "string"},
            "orchestrator": {"type": "string", "description": "What drove the harness, e.g. a CI job or agent framework."},
        })
    if name.endswith("dag_add_atom") or name.endswith("trace_append"):
        return schema({
            **common_dag,
            "atom_id": {"type": "string"},
            "kind": {"type": "string", "enum": list(ATOM_KINDS)},
            "summary": {"type": "string"},
            "content": {"type": "string"},
            "files": {"type": "array", "items": {"type": "string"}},
            "confidence": {
                "type": "string",
                "description": "Optional confidence value, normally a decimal string in [0.0, 1.0].",
            },
            "refs": {"type": "array", "items": {"type": "string"}},
            "attributes": {"type": "object", "additionalProperties": True},
            "links": {"type": "array", "items": {"type": "object"}},
        }, ["summary"])
    if name.endswith("dag_link"):
        return schema({**common_dag, "from": {"type": "string"}, "to": {"type": "string"}, "relation": {"type": "string"}, "label": {"type": "string"}}, ["from", "to"])
    if name.endswith("dag_ensure_session"):
        return schema({
            "project_path": {"type": "string"},
            "session_id": {"type": "string"},
            "host": {"type": "string"},
            "auto": {"type": "boolean"},
            "title": {"type": "string"},
            "objective": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
            "model": {"type": "string"},
            "orchestrator": {"type": "string", "description": "What drove the harness, e.g. a CI job or agent framework."},
        })
    if name.endswith("dag_finish_session"):
        return schema({
            "project_path": {"type": "string"},
            "session_id": {"type": "string"},
            "host": {"type": "string"},
            "summary": {"type": "string"},
            "kind": {"type": "string"},
            "outcome": {
                "type": "string",
                "enum": ["met", "unmet", "partially_met"],
                "description": "Explicit goal outcome. Omit to record only that the session ended.",
            },
        })
    if name.endswith("dag_list") or name.endswith("trace_list"):
        return schema({"project_path": {"type": "string"}, "limit": {"type": "integer"}})
    if name.endswith("dag_summary") or name.endswith("trace_summary") or name.endswith("dag_compact") or name.endswith("trace_compact"):
        return schema({**common_dag, "max_items": {"type": "integer"}, "focus_atom_id": {"type": "string"}})
    if name.endswith("dag_read") or name.endswith("trace_read"):
        return schema({**common_dag, "include_nodes": {"type": "boolean"}, "include_edges": {"type": "boolean"}, "limit": {"type": "integer"}})
    if name.endswith("dag_neighbors"):
        return schema({**common_dag, "atom_id": {"type": "string"}, "depth": {"type": "integer"}, "direction": {"type": "string"}}, ["atom_id"])
    if name.endswith("dag_frontier"):
        return schema({**common_dag, "kinds": {"type": "array", "items": {"type": "string"}}, "limit": {"type": "integer"}})
    if name.endswith("dag_search") or name.endswith("trace_search"):
        return schema({"query": {"type": "string"}, "project_path": {"type": "string"}, "limit": {"type": "integer"}}, ["query"])
    if name.endswith("dag_export") or name.endswith("trace_export"):
        return schema({**common_dag, "format": {"type": "string", "enum": ["markdown", "json", "xml"]}, "max_chars": {"type": "integer"}})
    if name.endswith("codex_import_thread"):
        return schema({
            **common_dag,
            "thread_id": {"type": "string"},
            "rollout_path": {"type": "string"},
            "codex_home": {"type": "string"},
            "run_id": {"type": "string"},
            "task_id": {"type": "string"},
            "title": {"type": "string"},
            "max_events": {"type": "integer"},
            "max_content_chars": {"type": "integer"},
            "include_canonical_events": {"type": "boolean"},
            "include_user_text": {"type": "boolean"},
            "include_agent_text": {"type": "boolean"},
            "include_tool_text": {"type": "boolean"},
            "include_instruction_text": {"type": "boolean"},
        })
    if name.endswith("lookup"):
        return schema({"term": {"type": "string"}}, ["term"])
    if name.endswith("lint_snippet"):
        return schema({
            "snippet": {"type": "string"},
            "mode": {"type": "string", "enum": ["full", "tag_balance"]},
        }, ["snippet"])
    if name.endswith("lint_trace"):
        return schema({"snippet": {"type": "string"}}, ["snippet"])
    return schema()


def list_tools():
    descriptions = {
        "scholia_dag_start": "Start a project-aware local Scholialang DAG in SQLite.",
        "scholia_dag_add_atom": "Add an atom node and optional edges to a local SQLite DAG.",
        "scholia_dag_set_model": "Record the model that produced a DAG (first writer wins unless overwrite).",
        "scholia_dag_link": "Create an explicit acyclic edge between two atoms.",
        "scholia_dag_ensure_session": "Idempotently find-or-create this session's per-project DAG (host-tagged, opt-out aware). Safe to call repeatedly.",
        "scholia_dag_finish_session": "Record session termination; provide outcome to append a goal-closing Concluding atom.",
        "scholia_dag_list": "List recent local DAGs.",
        "scholia_dag_summary": "Return a compact graph summary for token-efficient recall.",
        "scholia_dag_read": "Read bounded DAG metadata, nodes, and edges.",
        "scholia_dag_neighbors": "Read a bounded neighborhood around one atom.",
        "scholia_dag_frontier": "Return current graph frontier nodes.",
        "scholia_dag_search": "Search local DAG metadata and atoms.",
        "scholia_dag_compact": "Store and return a compact graph summary.",
        "scholia_dag_export": "Export a DAG as markdown, JSON, or XML.",
        "scholia_codex_import_thread": "Import a Codex rollout JSONL as an event-sourced Scholialang exhaust DAG.",
        "scholia_trace_start": "Compatibility alias for scholia_dag_start.",
        "scholia_trace_append": "Compatibility alias for scholia_dag_add_atom.",
        "scholia_trace_list": "Compatibility alias for scholia_dag_list.",
        "scholia_trace_summary": "Compatibility alias for scholia_dag_summary.",
        "scholia_trace_read": "Compatibility alias for scholia_dag_read.",
        "scholia_trace_search": "Compatibility alias for scholia_dag_search.",
        "scholia_trace_compact": "Compatibility alias for scholia_dag_compact.",
        "scholia_trace_export": "Compatibility alias for scholia_dag_export.",
        "scholia_catalog": "List Scholialang atoms, operators, relations, and resources.",
        "scholia_lookup": "Lookup a Scholialang atom, operator, or relation.",
        "scholia_lint_snippet": "Validate a Scholia snippet against the stable Scholia v0.6.2 language grammar using the 0.7.2 validator (closed-set atoms, canonical_id/fingerprint well-formedness, references, closure rules, and warnings). Pass mode='tag_balance' for the legacy tag-only check.",
        "scholia_lint_trace": "Validate a Scholia trace and return per-rule structured errors plus counts. Use for CI gates and dashboard rendering.",
    }
    return [
        {
            "name": name,
            "title": name.replace("scholia_", "").replace("_", " ").title(),
            "description": descriptions[name],
            "inputSchema": tool_schema(name),
        }
        for name in CANONICAL_TOOLS
    ]


def rpc_result(message_id, result):
    # 2026-07-28: every result carries ``resultType`` (SEP-2322) and the server
    # identifies itself in ``_meta`` (SEP-2575). Both are additive — earlier
    # protocol clients ignore the extra keys and, per spec, treat a missing
    # ``resultType`` as ``"complete"`` — so this stays dual-version.
    if isinstance(result, dict):
        result = dict(result)
        result.setdefault("resultType", "complete")
        meta = dict(result.get("_meta") or {})
        meta.setdefault(META_SERVER_INFO, server_info())
        result["_meta"] = meta
    return {"jsonrpc": "2.0", "id": message_id, "result": result}


def rpc_error(message_id, code, message, data=None):
    error = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": message_id, "error": error}


def dispatch(method, params):
    # 2026-07-28 MUST: advertise supported versions, capabilities, identity.
    # Also serves as the STDIO backward-compatibility probe for new hosts.
    if method == "server/discover":
        return {
            "supportedVersions": list(SUPPORTED_PROTOCOL_VERSIONS),
            "capabilities": {"tools": {}, "resources": {}},
            "instructions": "Use Scholialang DAG tools for explicit local SQLite work traces. Prefer summaries, frontier, search, and neighborhoods for token efficiency.",
            "ttlMs": CACHEABLE_TTL_MS,
            "cacheScope": CACHE_SCOPE,
        }
    # Legacy handshake — retained for pre-2026 hosts (dual-version). New hosts
    # never send it; they call server/discover and carry version in _meta.
    if method == "initialize":
        return {
            "protocolVersion": legacy_protocol_version(params),
            "capabilities": {"tools": {}, "resources": {}},
            "serverInfo": server_info(),
            "instructions": "Use Scholialang DAG tools for explicit local SQLite work traces. Prefer summaries, frontier, search, and neighborhoods for token efficiency.",
        }
    # ping was removed in 2026-07-28 but kept here as a no-op so pre-2026 hosts
    # that still heartbeat do not break.
    if method == "ping":
        return {}
    if method == "tools/list":
        # CacheableResult (SEP-2549): static catalog, private scope. Definition
        # order is the wire order — deterministic across calls and processes.
        return {"tools": list_tools(), "ttlMs": CACHEABLE_TTL_MS, "cacheScope": CACHE_SCOPE}
    if method == "tools/call":
        name = require_str(params, "name")
        args = params.get("arguments") or {}
        name = LEGACY_TOOL_ALIASES.get(name, name)
        if name not in CANONICAL_TOOLS:
            raise ValueError(f"unknown tool: {name}")
        return CANONICAL_TOOLS[name](args)
    if method == "resources/list":
        resources = []
        for uri, text in RESOURCE_TEXT.items():
            resources.append({"uri": uri, "name": uri.split("://", 1)[1], "mimeType": "text/markdown" if text.startswith("#") else "application/json"})
        return {"resources": resources, "ttlMs": CACHEABLE_TTL_MS, "cacheScope": CACHE_SCOPE}
    if method == "resources/read":
        uri = require_str(params, "uri")
        if uri not in RESOURCE_TEXT:
            raise ValueError(f"unknown resource: {uri}")
        text = RESOURCE_TEXT[uri]
        mime = "text/markdown" if text.startswith("#") else "application/json"
        return {
            "contents": [{"uri": uri, "mimeType": mime, "text": text}],
            "ttlMs": CACHEABLE_TTL_MS,
            "cacheScope": CACHE_SCOPE,
        }
    if method in {"resources/templates/list", "prompts/list"}:
        key = "resourceTemplates" if method.startswith("resources") else "prompts"
        return {key: [], "ttlMs": CACHEABLE_TTL_MS, "cacheScope": CACHE_SCOPE}
    raise NotImplementedError(method)


def handle_message(message):
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return rpc_error(message.get("id") if isinstance(message, dict) else None, -32600, "Invalid Request")
    method = message.get("method")
    message_id = message.get("id")
    if not method or message_id is None:
        return None
    params = message.get("params") or {}
    if not isinstance(params, dict):
        return rpc_error(message_id, -32602, "params must be a JSON object")
    meta = params.get("_meta")
    if meta is not None and not isinstance(meta, dict):
        return rpc_error(message_id, -32602, "_meta must be a JSON object")
    modern_version = modern_protocol_version(params)
    if modern_version is not None and modern_version != PROTOCOL_VERSION:
        return rpc_error(
            message_id,
            UNSUPPORTED_PROTOCOL_VERSION,
            "Unsupported protocol version",
            {
                "supported": list(SUPPORTED_PROTOCOL_VERSIONS),
                "requested": modern_version,
            },
        )
    if modern_version == PROTOCOL_VERSION:
        capabilities = (meta or {}).get(META_CLIENT_CAPABILITIES)
        if not isinstance(capabilities, dict):
            return rpc_error(
                message_id,
                -32602,
                f"missing or invalid required _meta field: {META_CLIENT_CAPABILITIES}",
            )
        client_info = (meta or {}).get(META_CLIENT_INFO)
        if client_info is not None and (
            not isinstance(client_info, dict)
            or not isinstance(client_info.get("name"), str)
            or not isinstance(client_info.get("version"), str)
        ):
            return rpc_error(
                message_id,
                -32602,
                f"invalid optional _meta field: {META_CLIENT_INFO}",
            )
        if method in {
            "initialize",
            "ping",
            "logging/setLevel",
            "resources/subscribe",
            "resources/unsubscribe",
        }:
            return rpc_error(message_id, -32601, f"Method not found: {method}")
    if method == "server/discover" and modern_version != PROTOCOL_VERSION:
        return rpc_error(
            message_id,
            -32602,
            "server/discover requires 2026-07-28 per-request _meta",
        )
    if method == "initialize":
        requested = legacy_protocol_version(params)
        legacy_versions = tuple(
            version for version in SUPPORTED_PROTOCOL_VERSIONS
            if version != PROTOCOL_VERSION
        )
        if requested not in legacy_versions:
            return rpc_error(
                message_id,
                UNSUPPORTED_PROTOCOL_VERSION,
                "Unsupported protocol version",
                {"supported": list(legacy_versions), "requested": requested or ""},
            )
    try:
        return rpc_result(message_id, dispatch(method, params))
    except NotImplementedError:
        return rpc_error(message_id, -32601, f"Method not found: {method}")
    except ValueError as exc:
        return rpc_error(message_id, -32602, str(exc))
    except Exception as exc:
        return rpc_error(message_id, -32603, "Internal error", {"detail": str(exc)})


FRAMING_NEWLINE = "newline"
FRAMING_HEADER = "content-length"


def read_message(stream=None):
    """Read one JSON-RPC message and report its framing.

    The MCP stdio transport is newline-delimited JSON-RPC: one JSON object per
    line, no embedded newlines. The repo's LSP path instead uses LSP-style
    ``Content-Length`` headers. Auto-detect from the first line so the same
    server serves MCP hosts (Claude Code, Codex, Ollama) and LSP clients.

    Returns ``(message, framing)`` or ``None`` at end of input.
    """
    stream = stream if stream is not None else sys.stdin.buffer
    first = stream.readline()
    if first == b"":
        return None
    # Tolerate stray blank lines between newline-framed messages.
    while first in (b"\r\n", b"\n"):
        first = stream.readline()
        if first == b"":
            return None
    if first.lstrip().lower().startswith(b"content-length:"):
        headers = {}
        line = first
        while line not in (b"\r\n", b"\n"):
            key, _, value = line.decode("ascii", "replace").partition(":")
            headers[key.lower()] = value.strip()
            line = stream.readline()
            if line == b"":
                break
        length = int(headers.get("content-length", "0"))
        if length <= 0:
            return None
        body = stream.read(length)
        return json.loads(body.decode("utf-8")), FRAMING_HEADER
    return json.loads(first.decode("utf-8")), FRAMING_NEWLINE


def send_message(message, framing=FRAMING_NEWLINE, stream=None):
    stream = stream if stream is not None else sys.stdout.buffer
    if framing == FRAMING_HEADER:
        body = json.dumps(message, separators=(",", ":"), sort_keys=True).encode("utf-8")
        stream.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body)
    else:
        body = json.dumps(message, separators=(",", ":")).encode("utf-8")
        stream.write(body + b"\n")
    stream.flush()


def main():
    while True:
        result = read_message()
        if result is None:
            break
        message, framing = result
        response = handle_message(message)
        if response is not None:
            send_message(response, framing)


if __name__ == "__main__":
    main()
