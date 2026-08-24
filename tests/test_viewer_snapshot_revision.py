"""The SSE poll loop must detect change cheaply.

Before this, ``send_events`` rebuilt a full snapshot — every DAG in the
project, every node and edge materialized — on every poll tick (4x/second per
connected stream) purely to compute a fingerprint it discarded when nothing
had changed. With enough DAGs and a couple of open tabs the viewer saturated
several cores and unrelated requests queued behind it for ~18s.

``snapshot_revision`` answers the same question with aggregate queries. The
load-bearing test here is ``test_revision_does_not_materialize_dags``: it fails
the moment the poll path starts walking nodes again.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins" / "claude-code" / "scholialang" / "scripts"

PROJECT = "/tmp/scholia-revision-test"


def _load(name: str):
    if str(SCRIPTS) not in sys.path:
        sys.path.insert(0, str(SCRIPTS))
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def viewer(tmp_path, monkeypatch):
    monkeypatch.setenv("SCHOLIALANG_HOME", str(tmp_path))
    _load("scholialang_mcp_server")
    return _load("scholialang_webview_server")


@pytest.fixture()
def dag_id(viewer):
    started = viewer.scholia.tool_dag_start(
        {"project_path": PROJECT, "title": "revision test", "objective": "measure"}
    )["structuredContent"]
    return started["dag_id"]


def _add_atom(viewer, dag_id, summary):
    viewer.scholia.tool_dag_add_atom(
        {"dag_id": dag_id, "project_path": PROJECT, "kind": "Observation", "summary": summary}
    )


def test_revision_is_stable_without_writes(viewer, dag_id):
    first = viewer.snapshot_revision(dag_id=dag_id, project_path=PROJECT)
    second = viewer.snapshot_revision(dag_id=dag_id, project_path=PROJECT)
    assert first == second


def test_revision_changes_when_an_atom_lands(viewer, dag_id):
    before = viewer.snapshot_revision(dag_id=dag_id, project_path=PROJECT)
    _add_atom(viewer, dag_id, "something happened")
    assert viewer.snapshot_revision(dag_id=dag_id, project_path=PROJECT) != before


def test_revision_changes_when_a_new_dag_appears(viewer, dag_id):
    before = viewer.snapshot_revision(dag_id=dag_id, project_path=PROJECT)
    viewer.scholia.tool_dag_start({"project_path": PROJECT, "title": "second", "objective": "o"})
    assert viewer.snapshot_revision(dag_id=dag_id, project_path=PROJECT) != before


def test_revision_tracks_the_project_when_no_dag_is_selected(viewer, dag_id):
    before = viewer.snapshot_revision(dag_id=None, project_path=PROJECT)
    _add_atom(viewer, dag_id, "still counts")
    assert viewer.snapshot_revision(dag_id=None, project_path=PROJECT) != before


def test_revision_survives_an_unknown_dag_id(viewer, dag_id):
    # A stale dag_id in a bookmarked URL must not raise inside the poll loop.
    assert viewer.snapshot_revision(dag_id="dag_does_not_exist", project_path=PROJECT) is not None


def test_revision_does_not_materialize_dags(viewer, dag_id, monkeypatch):
    """The regression guard: the poll path must never walk nodes and edges."""
    def explode(*args, **kwargs):
        raise AssertionError("poll path materialized every DAG again")

    monkeypatch.setattr(viewer.scholia, "all_dags", explode)
    monkeypatch.setattr(viewer.scholia, "row_to_dag", explode)
    monkeypatch.setattr(viewer, "load_snapshot", explode)
    assert viewer.snapshot_revision(dag_id=dag_id, project_path=PROJECT) is not None
