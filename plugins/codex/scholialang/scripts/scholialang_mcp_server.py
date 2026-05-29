#!/usr/bin/env python3
import hashlib
import html
import json
import os
import re
import secrets
import sqlite3
import sys
from collections import deque
from datetime import datetime, timezone
from pathlib import Path


PROTOCOL_VERSION = "2025-11-25"
SERVER_NAME = "scholialang"
SERVER_VERSION = "0.1.0"
MAX_TEXT = 6000

ATOM_KINDS = [
    "Hypothesis",
    "Observation",
    "Evidence",
    "Finding",
    "Deciding",
    "Action",
    "Contradiction",
    "Retract",
    "Summary",
]

ATOMS = [
    {"id": "hypothesis", "tag": "Hypothesis", "summary": "A proposition the agent will test."},
    {"id": "observation", "tag": "Observation", "summary": "External input from a command, file, query, or review."},
    {"id": "evidence", "tag": "Evidence", "summary": "Material that supports, refutes, or qualifies a hypothesis."},
    {"id": "finding", "tag": "Finding", "summary": "A conclusion drawn from available evidence."},
    {"id": "decision", "tag": "Deciding", "summary": "A branch point and selected path."},
    {"id": "action", "tag": "Action", "summary": "A durable external state change."},
    {"id": "contradiction", "tag": "Contradiction", "summary": "Two trace claims that cannot both be true."},
    {"id": "retraction", "tag": "Retract", "summary": "Explicit revocation of a prior finding."},
    {"id": "summary", "tag": "Summary", "summary": "A compact restatement of graph state."},
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
    init_db(conn)
    return conn


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

        CREATE INDEX IF NOT EXISTS idx_dags_project_updated ON dags(project_key, updated_at DESC);
        CREATE INDEX IF NOT EXISTS idx_nodes_dag_ordinal ON nodes(dag_id, ordinal);
        CREATE INDEX IF NOT EXISTS idx_edges_dag_from ON edges(dag_id, from_atom_id);
        CREATE INDEX IF NOT EXISTS idx_edges_dag_to ON edges(dag_id, to_atom_id);
        """
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


def dag_metadata(dag):
    return {
        "dag_id": dag["dag_id"],
        "trace_id": dag["dag_id"],
        "title": dag.get("title", ""),
        "objective": dag.get("objective", ""),
        "tags": dag.get("tags", []),
        "project_path": dag.get("project_path"),
        "project_name": dag.get("project_name"),
        "project_key": dag.get("project_key"),
        "created_at": dag.get("created_at"),
        "updated_at": dag.get("updated_at"),
        "node_count": len(dag.get("nodes", {})),
        "edge_count": len(dag.get("edges", [])),
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
        "decision": "Deciding",
        "deciding": "Deciding",
        "retraction": "Retract",
        "retract": "Retract",
    }
    if raw.lower() in aliases:
        return aliases[raw.lower()]
    for known in ATOM_KINDS:
        if raw.lower() == known.lower():
            return known
    return re.sub(r"[^A-Za-z0-9_-]", "", raw) or "Finding"


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
        conn.commit()
        dag = load_dag_conn(conn, dag_id)
        structured = dag_metadata(dag)
        return content_result(f"Started Scholialang SQLite DAG {dag_id} for {info['project_name']}.", structured)
    finally:
        conn.close()


def tool_dag_add_atom(args):
    dag_id = dag_id_arg(args)
    conn = connect()
    try:
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
            "created_at": now(),
        }
        ordinal = len(dag["order"]) + 1
        conn.execute(
            """
            INSERT INTO nodes (
              dag_id, atom_id, ordinal, kind, summary, content,
              files_json, confidence_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
    items = [dag_metadata(dag) for dag in all_dags(args.get("project_path"))[:limit]]
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
    frontier = frontier_nodes(dag, kind_filter=["Finding", "Deciding", "Action", "Summary"])[:max_items]
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
        rows = [f'<Trace id="{html.escape(dag["dag_id"])}" title="{html.escape(dag.get("title", ""))}">']
        for node_id in dag.get("order", []):
            node = dag["nodes"][node_id]
            kind = html.escape(node.get("kind", "Atom"))
            rows.append(f'  <{kind} id="{html.escape(node_id)}">')
            rows.append(f'    <Summary>{html.escape(node.get("summary", ""))}</Summary>')
            if node.get("content"):
                rows.append(f'    <Content>{html.escape(node.get("content", ""))}</Content>')
            rows.append(f"  </{kind}>")
        for edge in dag.get("edges", []):
            rows.append(f'  <Edge from="{html.escape(edge["from"])}" to="{html.escape(edge["to"])}" relation="{html.escape(edge["relation"])}"/>')
        rows.append("</Trace>")
        text = "\n".join(rows)
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
    return content_result(compact_text(text, int(args.get("max_chars", 20000))), {"dag_id": dag["dag_id"], "format": export_format})


def tool_catalog(_args):
    structured = {
        "atoms": ATOMS,
        "operators": OPERATORS,
        "relations": RELATIONS,
        "resources": list(RESOURCE_TEXT),
        "database_path": str(database_path()),
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


def tool_lint_snippet(args):
    snippet = require_str(args, "snippet")
    errors = []
    stack = []
    for match in re.finditer(r"<(/?)([A-Za-z][A-Za-z0-9_-]*)([^<>]*?)(/?)>", snippet):
        closing, tag, attrs, self_closing = match.groups()
        if self_closing or attrs.strip().endswith("/"):
            continue
        if closing:
            if not stack:
                errors.append(f"closing tag without opener: {tag}")
            else:
                opened = stack.pop()
                if opened != tag:
                    errors.append(f"tag mismatch: opened {opened}, closed {tag}")
        else:
            stack.append(tag)
    for tag in reversed(stack):
        errors.append(f"unclosed tag: {tag}")
    result = {"ok": not errors, "errors": errors}
    return content_result(json.dumps(result, indent=2), result, bool(errors))


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
        return "Summary"
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
            "source": "opentalon-stage:rsi_codex_parser",
            "rollout_line": line_no,
            "raw_line_sha256": sha256_text(raw_line),
        },
    }
    return json.dumps(content, indent=2, sort_keys=True)


def tool_codex_import_thread(args):
    home = codex_home(args)
    project_path = args.get("project_path")
    thread_id = args.get("thread_id")
    thread_row = load_codex_thread_row(home, thread_id, project_path)
    if thread_row is not None:
        thread_id = thread_row["id"]

    rollout_path = args.get("rollout_path")
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
        load_dag(dag_id, project_path)
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
        "canonical_policy": "OpenTalon stage rsi_codex_parser parity: preserve raw rollout atoms and derive task_message/task_tool_call/task_tool_result/token_usage/task_output envelopes.",
    }
    if thread_row is not None:
        for key in ("title", "cwd", "model", "reasoning_effort", "source", "thread_source", "tokens_used"):
            if key in thread_row.keys():
                metadata[key] = thread_row[key]

    root_atom = tool_dag_add_atom(
        {
            "dag_id": dag_id,
            "project_path": project_path,
            "kind": "Observation",
            "summary": f"Codex rollout source resolved for {thread_id or path.name}.",
            "content": json.dumps(metadata, indent=2, sort_keys=True),
            "files": [str(path)],
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
                        "label": "canonical OpenTalon-style event derived from raw Codex rollout event",
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
                "kind": "Summary",
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
    tool_dag_add_atom(
        {
            "dag_id": dag_id,
            "project_path": project_path,
            "kind": "Summary",
            "summary": f"Imported {imported} Codex rollout events into an observable exhaust trail.",
            "content": json.dumps(final_summary, indent=2, sort_keys=True),
            "files": [str(path)],
            "links": [{"to": previous_atom_id, "relation": "after"}],
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


TOOLS = {
    "scholia.dag_start": tool_dag_start,
    "scholia.dag_add_atom": tool_dag_add_atom,
    "scholia.dag_link": tool_dag_link,
    "scholia.dag_list": tool_dag_list,
    "scholia.dag_summary": tool_dag_summary,
    "scholia.dag_read": tool_dag_read,
    "scholia.dag_neighbors": tool_dag_neighbors,
    "scholia.dag_frontier": tool_dag_frontier,
    "scholia.dag_search": tool_dag_search,
    "scholia.dag_compact": tool_dag_compact,
    "scholia.dag_export": tool_dag_export,
    "scholia.codex_import_thread": tool_codex_import_thread,
    "scholia.trace_start": tool_dag_start,
    "scholia.trace_append": tool_dag_add_atom,
    "scholia.trace_list": tool_dag_list,
    "scholia.trace_summary": tool_dag_summary,
    "scholia.trace_read": tool_dag_read,
    "scholia.trace_search": tool_dag_search,
    "scholia.trace_compact": tool_dag_compact,
    "scholia.trace_export": tool_dag_export,
    "scholia.catalog": tool_catalog,
    "scholia.lookup": tool_lookup,
    "scholia.lint_snippet": tool_lint_snippet,
}


def schema(properties=None, required=None):
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": properties or {},
        "required": required or [],
    }


def tool_schema(name):
    common_dag = {"dag_id": {"type": "string"}, "trace_id": {"type": "string"}, "project_path": {"type": "string"}}
    if name.endswith("dag_start") or name.endswith("trace_start"):
        return schema({
            "project_path": {"type": "string"},
            "title": {"type": "string"},
            "objective": {"type": "string"},
            "tags": {"type": "array", "items": {"type": "string"}},
        })
    if name.endswith("dag_add_atom") or name.endswith("trace_append"):
        return schema({
            **common_dag,
            "atom_id": {"type": "string"},
            "kind": {"type": "string"},
            "summary": {"type": "string"},
            "content": {"type": "string"},
            "files": {"type": "array", "items": {"type": "string"}},
            "confidence": {"type": ["number", "string", "null"]},
            "refs": {"type": "array", "items": {"type": "string"}},
            "links": {"type": "array", "items": {"type": "object"}},
        }, ["summary"])
    if name.endswith("dag_link"):
        return schema({**common_dag, "from": {"type": "string"}, "to": {"type": "string"}, "relation": {"type": "string"}, "label": {"type": "string"}}, ["from", "to"])
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
        return schema({"snippet": {"type": "string"}}, ["snippet"])
    return schema()


def list_tools():
    descriptions = {
        "scholia.dag_start": "Start a project-aware local Scholialang DAG in SQLite.",
        "scholia.dag_add_atom": "Add an atom node and optional edges to a local SQLite DAG.",
        "scholia.dag_link": "Create an explicit acyclic edge between two atoms.",
        "scholia.dag_list": "List recent local DAGs.",
        "scholia.dag_summary": "Return a compact graph summary for token-efficient recall.",
        "scholia.dag_read": "Read bounded DAG metadata, nodes, and edges.",
        "scholia.dag_neighbors": "Read a bounded neighborhood around one atom.",
        "scholia.dag_frontier": "Return current graph frontier nodes.",
        "scholia.dag_search": "Search local DAG metadata and atoms.",
        "scholia.dag_compact": "Store and return a compact graph summary.",
        "scholia.dag_export": "Export a DAG as markdown, JSON, or XML.",
        "scholia.codex_import_thread": "Import a Codex rollout JSONL as an event-sourced Scholialang exhaust DAG.",
        "scholia.trace_start": "Compatibility alias for scholia.dag_start.",
        "scholia.trace_append": "Compatibility alias for scholia.dag_add_atom.",
        "scholia.trace_list": "Compatibility alias for scholia.dag_list.",
        "scholia.trace_summary": "Compatibility alias for scholia.dag_summary.",
        "scholia.trace_read": "Compatibility alias for scholia.dag_read.",
        "scholia.trace_search": "Compatibility alias for scholia.dag_search.",
        "scholia.trace_compact": "Compatibility alias for scholia.dag_compact.",
        "scholia.trace_export": "Compatibility alias for scholia.dag_export.",
        "scholia.catalog": "List Scholialang atoms, operators, relations, and resources.",
        "scholia.lookup": "Lookup a Scholialang atom, operator, or relation.",
        "scholia.lint_snippet": "Run lightweight XML-like tag checks on a snippet.",
    }
    return [
        {
            "name": name,
            "title": name.replace("scholia.", "").replace("_", " ").title(),
            "description": descriptions[name],
            "inputSchema": tool_schema(name),
        }
        for name in TOOLS
    ]


def rpc_result(message_id, result):
    return {"jsonrpc": "2.0", "id": message_id, "result": result}


def rpc_error(message_id, code, message, data=None):
    error = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": message_id, "error": error}


def dispatch(method, params):
    if method == "initialize":
        return {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}, "resources": {}},
            "serverInfo": {"name": SERVER_NAME, "title": "Scholialang", "version": SERVER_VERSION},
            "instructions": "Use Scholialang DAG tools for explicit local SQLite work traces. Prefer summaries, frontier, search, and neighborhoods for token efficiency.",
        }
    if method == "ping":
        return {}
    if method == "tools/list":
        return {"tools": list_tools()}
    if method == "tools/call":
        name = require_str(params, "name")
        args = params.get("arguments") or {}
        if name not in TOOLS:
            raise ValueError(f"unknown tool: {name}")
        return TOOLS[name](args)
    if method == "resources/list":
        resources = []
        for uri, text in RESOURCE_TEXT.items():
            resources.append({"uri": uri, "name": uri.split("://", 1)[1], "mimeType": "text/markdown" if text.startswith("#") else "application/json"})
        return {"resources": resources}
    if method == "resources/read":
        uri = require_str(params, "uri")
        if uri not in RESOURCE_TEXT:
            raise ValueError(f"unknown resource: {uri}")
        text = RESOURCE_TEXT[uri]
        mime = "text/markdown" if text.startswith("#") else "application/json"
        return {"contents": [{"uri": uri, "mimeType": mime, "text": text}]}
    if method in {"resources/templates/list", "prompts/list"}:
        key = "resourceTemplates" if method.startswith("resources") else "prompts"
        return {key: []}
    raise NotImplementedError(method)


def handle_message(message):
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0":
        return rpc_error(message.get("id") if isinstance(message, dict) else None, -32600, "Invalid Request")
    method = message.get("method")
    message_id = message.get("id")
    if not method or message_id is None:
        return None
    params = message.get("params") or {}
    try:
        return rpc_result(message_id, dispatch(method, params))
    except NotImplementedError:
        return rpc_error(message_id, -32601, f"Method not found: {method}")
    except ValueError as exc:
        return rpc_error(message_id, -32602, str(exc))
    except Exception as exc:
        return rpc_error(message_id, -32603, "Internal error", {"detail": str(exc)})


def read_framed_message():
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if line == b"":
            return None
        if line in (b"\r\n", b"\n"):
            break
        key, _, value = line.decode("ascii", "replace").partition(":")
        headers[key.lower()] = value.strip()
    length = int(headers.get("content-length", "0"))
    if length <= 0:
        return None
    body = sys.stdin.buffer.read(length)
    return json.loads(body.decode("utf-8"))


def send_framed_message(message):
    body = json.dumps(message, separators=(",", ":"), sort_keys=True).encode("utf-8")
    sys.stdout.buffer.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body)
    sys.stdout.buffer.flush()


def main():
    while True:
        message = read_framed_message()
        if message is None:
            break
        response = handle_message(message)
        if response is not None:
            send_framed_message(response)


if __name__ == "__main__":
    main()
