"""Model provenance on DAGs: schema migration, resolution, and transcript capture.

Covers the column added by ``init_db``, the ``model:<id>`` tag fallback that lets
externally-ingested traces declare provenance without a schema write, the
first-writer-wins stamp, and the transcript scan the tailer backfills from.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins" / "claude-code" / "scholialang" / "scripts"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def server(tmp_path, monkeypatch):
    monkeypatch.setenv("SCHOLIALANG_HOME", str(tmp_path))
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    return _load("scholialang_mcp_server")


@pytest.fixture()
def conn(server):
    connection = server.connect()
    yield connection
    connection.close()


def _make_dag(server, conn, dag_id="dag_test", model=None):
    now = server.now()
    info = server.project_info("/tmp/scholia-model-test")
    server.upsert_project(conn, info)
    conn.execute(
        "INSERT INTO dags (dag_id, title, objective, tags_json, project_key, "
        "project_path, project_name, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (dag_id, "t", "o", "[]", info["project_key"], info["project_path"],
         info["project_name"], now, now),
    )
    if model:
        conn.execute("UPDATE dags SET model = ? WHERE dag_id = ?", (model, dag_id))
    conn.commit()
    return dag_id


# --- schema -----------------------------------------------------------------

def test_migration_adds_model_column(conn):
    cols = {row[1] for row in conn.execute("PRAGMA table_info(dags)")}
    assert "model" in cols


def test_migration_is_idempotent_on_an_existing_db(server, conn):
    # init_db runs on every connect(); a second pass must not raise.
    again = server.connect()
    cols = {row[1] for row in again.execute("PRAGMA table_info(dags)")}
    again.close()
    assert "model" in cols


# --- resolution -------------------------------------------------------------

def test_stored_column_wins(server):
    assert server.dag_model({"model": "claude-opus-5", "tags": ["model:ignored"]}) == "claude-opus-5"


def test_falls_back_to_model_tag(server):
    assert server.dag_model({"model": None, "tags": ["autoemit", "model:qwen3.8:27b-q8_0"]}) == "qwen3.8:27b-q8_0"


def test_returns_none_without_column_or_tag(server):
    assert server.dag_model({"tags": ["autoemit", "host:claude-code"]}) is None


def test_ignores_a_bare_or_empty_model_tag(server):
    assert server.dag_model({"tags": ["model:", "model:   "]}) is None


def test_tolerates_non_string_tags(server):
    assert server.dag_model({"tags": [None, 7, {"model": "x"}, "model:sonnet"]}) == "sonnet"


# --- stamping ---------------------------------------------------------------

def test_first_writer_wins(server, conn):
    dag_id = _make_dag(server, conn)
    assert server.set_dag_model(conn, dag_id, "first") == "first"
    assert server.set_dag_model(conn, dag_id, "second") == "first"


def test_overwrite_replaces(server, conn):
    dag_id = _make_dag(server, conn, model="first")
    assert server.set_dag_model(conn, dag_id, "second", overwrite=True) == "second"


def test_empty_model_is_a_noop(server, conn):
    dag_id = _make_dag(server, conn)
    assert server.set_dag_model(conn, dag_id, "") is None
    assert server.set_dag_model(conn, dag_id, None) is None


def test_metadata_surfaces_the_model(server, conn):
    dag_id = _make_dag(server, conn, model="claude-opus-5")
    dag = server.load_dag_conn(conn, dag_id)
    assert server.dag_metadata(dag)["model"] == "claude-opus-5"


def test_set_model_tool_round_trips(server, conn):
    dag_id = _make_dag(server, conn)
    result = server.tool_dag_set_model({"dag_id": dag_id, "model": "qwen3.8:27b-q8_0"})
    assert result["structuredContent"]["model"] == "qwen3.8:27b-q8_0"


# --- transcript capture -----------------------------------------------------

def test_model_from_lines_takes_the_first_assistant_model():
    cc = _load("cc_exhaust")
    lines = [
        '{"type": "user", "message": {"role": "user"}}',
        '{"type": "assistant", "message": {"role": "assistant", "model": "claude-opus-5"}}',
        '{"type": "assistant", "message": {"role": "assistant", "model": "claude-haiku-4-5"}}',
    ]
    assert cc.model_from_lines(lines) == "claude-opus-5"


def test_model_from_lines_skips_malformed_and_modelless_records():
    cc = _load("cc_exhaust")
    lines = [
        "",
        "not json at all",
        '{"type": "assistant"}',
        '{"type": "assistant", "message": {"role": "assistant", "model": "   "}}',
        '{"type": "assistant", "message": {"role": "assistant", "model": "sonnet"}}',
    ]
    assert cc.model_from_lines(lines) == "sonnet"


def test_model_from_lines_returns_none_when_no_assistant_turn_exists():
    cc = _load("cc_exhaust")
    # This is the SessionStart case: the transcript exists but has no answer yet.
    assert cc.model_from_lines(['{"type": "user", "message": {"role": "user"}}']) is None


# --- harness (stream kind stripped) -----------------------------------------

def test_harness_strips_the_exhaust_suffix(server):
    assert server.dag_harness({"session_key": "claude-code-exhaust:abc"}) == "claude-code"


def test_harness_leaves_a_checkpoint_host_alone(server):
    assert server.dag_harness({"session_key": "claude-code:abc"}) == "claude-code"


def test_paired_streams_report_the_same_harness(server):
    checkpoint = server.dag_harness({"session_key": "codex:s1"})
    exhaust = server.dag_harness({"session_key": "codex-exhaust:s1"})
    assert checkpoint == exhaust == "codex"


def test_harness_is_none_without_a_session_key(server):
    assert server.dag_harness({}) is None
    assert server.dag_harness({"session_key": ""}) is None


def test_metadata_carries_both_host_and_harness(server, conn):
    dag_id = _make_dag(server, conn)
    conn.execute(
        "UPDATE dags SET session_key = ? WHERE dag_id = ?", ("claude-code-exhaust:s1", dag_id)
    )
    conn.commit()
    meta = server.dag_metadata(server.load_dag_conn(conn, dag_id))
    # host stays truthful to storage; harness answers "who produced this".
    assert meta["host"] == "claude-code-exhaust"
    assert meta["harness"] == "claude-code"


# --- placeholder models -----------------------------------------------------

def test_model_from_lines_skips_synthetic_placeholder():
    cc = _load("cc_exhaust")
    lines = [
        '{"type": "assistant", "message": {"role": "assistant", "model": "<synthetic>"}}',
        '{"type": "assistant", "message": {"role": "assistant", "model": "claude-opus-5"}}',
    ]
    assert cc.model_from_lines(lines) == "claude-opus-5"


def test_model_from_lines_returns_none_when_only_placeholders_exist():
    cc = _load("cc_exhaust")
    assert cc.model_from_lines(
        ['{"type": "assistant", "message": {"role": "assistant", "model": "<synthetic>"}}']
    ) is None

