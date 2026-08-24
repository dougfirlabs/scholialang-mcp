"""Orchestrator provenance: what drove the harness, when something else did.

Three facts want three fields — `harness` is what ran the model, `model` is
what was sampled, `orchestrator` is what invoked the harness. Overloading one
slot is what put machine names in `host` in real traces, so the guard here is
as load-bearing as the feature.
"""
from __future__ import annotations

import importlib.util
import platform
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins" / "claude-code" / "scholialang" / "scripts"
PROJECT = "/tmp/scholia-orchestrator-test"


def _load(name: str):
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("SCHOLIALANG_HOME", str(tmp_path))
    monkeypatch.delenv("SCHOLIA_ORCHESTRATOR", raising=False)
    monkeypatch.delenv("SCHOLIA_HOST", raising=False)
    return _load("scholialang_mcp_server")


# --- schema -----------------------------------------------------------------

def test_migration_adds_orchestrator_column(server):
    conn = server.connect()
    cols = {row[1] for row in conn.execute("PRAGMA table_info(dags)")}
    conn.close()
    assert "orchestrator" in cols


# --- resolution -------------------------------------------------------------

def test_stored_column_wins(server):
    assert server.dag_orchestrator({"orchestrator": "ci-runner", "tags": ["orchestrator:other"]}) == "ci-runner"


def test_falls_back_to_tag(server):
    assert server.dag_orchestrator({"tags": ["autoemit", "orchestrator:nightly-sweep"]}) == "nightly-sweep"


def test_absent_when_nobody_declared_one(server):
    assert server.dag_orchestrator({"tags": ["autoemit", "host:claude-code"]}) is None


def test_tag_carrying_a_machine_name_is_ignored(server):
    assert server.dag_orchestrator({"tags": [f"orchestrator:{platform.node()}"]}) is None


# --- the hostname guard -----------------------------------------------------

def test_machine_identity_detects_fqdn_and_short_form(server):
    node = platform.node()
    assert server._is_machine_identity(node)
    assert server._is_machine_identity(node.split(".", 1)[0])
    assert server._is_machine_identity(node.upper())


def test_machine_identity_ignores_real_names(server):
    assert not server._is_machine_identity("claude-code")
    assert not server._is_machine_identity("")
    assert not server._is_machine_identity(None)


def test_declared_orchestrator_rejects_a_machine_name(server, monkeypatch):
    monkeypatch.setenv("SCHOLIA_ORCHESTRATOR", platform.node())
    assert server.requested_orchestrator({}) is None


def test_declared_orchestrator_reads_the_env(server, monkeypatch):
    monkeypatch.setenv("SCHOLIA_ORCHESTRATOR", "nightly-sweep")
    assert server.requested_orchestrator({}) == "nightly-sweep"


def test_explicit_arg_beats_the_env(server, monkeypatch):
    monkeypatch.setenv("SCHOLIA_ORCHESTRATOR", "from-env")
    assert server.requested_orchestrator({"orchestrator": "from-arg"}) == "from-arg"


def test_host_falls_back_to_mcp_when_given_a_machine_name(server, monkeypatch):
    """The regression this guard exists for: real traces carry a laptop name as host."""
    monkeypatch.setenv("SCHOLIA_HOST", platform.node())
    host, _ = server.requested_session_identity({})
    assert host == "mcp"


def test_host_keeps_a_real_harness_name(server, monkeypatch):
    monkeypatch.setenv("SCHOLIA_HOST", "claude-code")
    host, _ = server.requested_session_identity({})
    assert host == "claude-code"


# --- end to end -------------------------------------------------------------

def test_dag_start_records_a_declared_orchestrator(server, monkeypatch):
    monkeypatch.setenv("SCHOLIA_ORCHESTRATOR", "nightly-sweep")
    dag_id = server.tool_dag_start(
        {"project_path": PROJECT, "title": "t", "objective": "o"}
    )["structuredContent"]["dag_id"]
    meta = server.dag_metadata(server.load_dag(dag_id, PROJECT))
    assert meta["orchestrator"] == "nightly-sweep"


def test_dag_start_leaves_it_null_when_undeclared(server):
    dag_id = server.tool_dag_start(
        {"project_path": PROJECT, "title": "t", "objective": "o"}
    )["structuredContent"]["dag_id"]
    meta = server.dag_metadata(server.load_dag(dag_id, PROJECT))
    assert meta["orchestrator"] is None


def test_session_dag_records_orchestrator_alongside_harness(server, monkeypatch):
    monkeypatch.setenv("SCHOLIA_ORCHESTRATOR", "nightly-sweep")
    started = server.tool_dag_ensure_session(
        {"project_path": PROJECT, "session_id": "s1", "host": "claude-code", "auto": True}
    )["structuredContent"]
    meta = server.dag_metadata(server.load_dag(started["dag_id"], PROJECT))
    assert (meta["harness"], meta["orchestrator"]) == ("claude-code", "nightly-sweep")


def test_first_writer_wins(server):
    conn = server.connect()
    dag_id = server.tool_dag_start(
        {"project_path": PROJECT, "title": "t", "objective": "o"}
    )["structuredContent"]["dag_id"]
    assert server.set_dag_orchestrator(conn, dag_id, "first") == "first"
    assert server.set_dag_orchestrator(conn, dag_id, "second") == "first"
    assert server.set_dag_orchestrator(conn, dag_id, "second", overwrite=True) == "second"
    conn.close()
