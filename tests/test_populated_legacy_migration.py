"""Upgrade seeded pre-provenance databases without reading an operator database."""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("host", ["claude-code", "codex", "ollama"])
def test_populated_legacy_database_upgrade_is_lossless_and_idempotent(
    host, tmp_path, monkeypatch
):
    monkeypatch.setenv("SCHOLIALANG_HOME", str(tmp_path))
    scripts = ROOT / "plugins" / host / "scholialang" / "scripts"
    monkeypatch.syspath_prepend(str(scripts))
    name = "legacy_migration_" + host.replace("-", "_")
    spec = importlib.util.spec_from_file_location(name, scripts / "scholialang_mcp_server.py")
    server = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, server)
    spec.loader.exec_module(server)

    # Deliberately create the historical schema ourselves, before connect() can
    # run any candidate migration. All rows and identities below are synthetic.
    with sqlite3.connect(server.database_path()) as old:
        old.executescript("""
            CREATE TABLE projects (project_key TEXT PRIMARY KEY, project_path TEXT,
                project_name TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE dags (dag_id TEXT PRIMARY KEY, title TEXT NOT NULL,
                objective TEXT NOT NULL, tags_json TEXT NOT NULL, project_key TEXT NOT NULL,
                project_path TEXT, project_name TEXT NOT NULL, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL, session_key TEXT);
            CREATE TABLE nodes (dag_id TEXT NOT NULL, atom_id TEXT NOT NULL,
                ordinal INTEGER NOT NULL, kind TEXT NOT NULL, summary TEXT NOT NULL,
                content TEXT NOT NULL, files_json TEXT NOT NULL, confidence_json TEXT,
                attrs_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL,
                PRIMARY KEY (dag_id, atom_id));
            CREATE TABLE edges (id INTEGER PRIMARY KEY AUTOINCREMENT, dag_id TEXT NOT NULL,
                from_atom_id TEXT NOT NULL, to_atom_id TEXT NOT NULL, relation TEXT NOT NULL,
                label TEXT NOT NULL, created_at TEXT NOT NULL,
                UNIQUE (dag_id, from_atom_id, to_atom_id, relation, label));
        """)
        stamp = "2020-01-01T00:00:00Z"
        old.execute("INSERT INTO projects VALUES (?, ?, ?, ?, ?)",
                    ("synthetic", None, "synthetic", stamp, stamp))
        tags = ["model:fixture-model", "orchestrator:fixture-runner"]
        old.execute("INSERT INTO dags VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    ("dag_legacy", "Synthetic migration", "Preserve rows", json.dumps(tags),
                     "synthetic", None, "synthetic", stamp, stamp, "codex:fixture"))
        for ordinal, kind in enumerate(("Goal", "Observation"), 1):
            old.execute("INSERT INTO nodes VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        ("dag_legacy", kind + "_0001", ordinal, kind, "Synthetic",
                         "Fixture content — preserved", "[]", None, "{}", stamp))
        old.execute("INSERT INTO edges VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (1, "dag_legacy", "Observation_0001", "Goal_0001", "refers", "", stamp))
        tables = ("projects", "dags", "nodes", "edges")
        columns = {table: [row[1] for row in old.execute(f"PRAGMA table_info({table})")]
                   for table in tables}
        assert {"model", "orchestrator"}.isdisjoint(columns["dags"])
        before = {table: old.execute(f"SELECT * FROM {table} ORDER BY 1, 2").fetchall()
                  for table in tables}

    for _ in range(2):
        connection = server.connect()
        try:
            migrated = {row[1]: row for row in connection.execute("PRAGMA table_info(dags)")}
            for field in ("model", "orchestrator"):
                assert field in migrated and migrated[field][3] == 0  # nullable
            for table in tables:
                selection = ", ".join(columns[table])
                actual = connection.execute(f"SELECT {selection} FROM {table} ORDER BY 1, 2")
                assert [tuple(row) for row in actual] == before[table]
            assert tuple(connection.execute("SELECT model, orchestrator FROM dags").fetchone()) == (None, None)
            assert not connection.execute("PRAGMA foreign_key_check").fetchall()
        finally:
            connection.close()
        metadata = server.dag_metadata(server.load_dag("dag_legacy"))
        assert metadata["model"] == "fixture-model"
        assert metadata["orchestrator"] == "fixture-runner"
        assert metadata["harness"] == "codex"
        assert metadata["tags"] == tags
        assert (metadata["node_count"], metadata["edge_count"]) == (2, 1)
