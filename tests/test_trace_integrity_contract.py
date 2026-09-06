"""Trace-integrity contract promoted from the #46 review probes.

The independent review ran 36 synthetic probes across all three bundles
(12 per bundle):
nine metadata list/read parity cases, nine premise-free closure cases, nine
supported-closure cases, three no-outcome lifecycle cases, and six invalid
outcome/kind combinations. This file keeps every one of them as a permanent
test across all three byte-identical bundles, plus the adversarial coverage
the review inventory found missing: concurrent atom IDs/ordinals, concurrent
opposite links, rollback on rejected mutation, and live opt-out/resume.

The revised closure contract is deliberate and documented (README): without
an outcome, session finish stays a lifecycle Observation; with an outcome, a
Concluding must cite a genuine in-trace Finding/Observation/Evidence premise
or the call is rejected before any atom, edge, counter, or session-binding
mutation. The nine premise-free probes are therefore rejection/no-mutation
tests here, not resurrected as passing old-contract tests.
"""
from __future__ import annotations

import importlib.util
import sqlite3
import threading
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BUNDLES = ("claude-code", "codex", "ollama")
OUTCOMES = ("met", "unmet", "partially_met")
PROBE_TAGS = ["probe-tag", "model:probe-model", "orchestrator:probe-orchestrator"]

_MODULES: dict[str, object] = {}


def _load_bundle(bundle: str):
    if bundle not in _MODULES:
        path = ROOT / "plugins" / bundle / "scholialang" / "scripts" / "scholialang_mcp_server.py"
        name = "integrity_contract_server_" + bundle.replace("-", "_")
        spec = importlib.util.spec_from_file_location(name, path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _MODULES[bundle] = module
    return _MODULES[bundle]


@pytest.fixture(params=BUNDLES, ids=BUNDLES)
def server(request, tmp_path, monkeypatch):
    monkeypatch.setenv("SCHOLIALANG_HOME", str(tmp_path))
    monkeypatch.delenv("SCHOLIA_ORCHESTRATOR", raising=False)
    monkeypatch.delenv("SCHOLIA_HOST", raising=False)
    monkeypatch.delenv("SCHOLIA_AUTOEMIT", raising=False)
    return _load_bundle(request.param)


@pytest.fixture()
def project(tmp_path):
    return str(tmp_path / "project")


def _snapshot(server):
    """Full logical dump of every table, hashable for no-mutation assertions."""
    with sqlite3.connect(server.database_path()) as conn:
        tables = [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        ]
        return {
            name: sorted(map(repr, conn.execute('SELECT * FROM "' + name + '"')))
            for name in tables
        }


def _identity(project):
    return {
        "project_path": project,
        "host": "contract-host",
        "session_id": "contract-session",
        "auto": False,
    }


def _ensure(server, project):
    return server.tool_dag_ensure_session(
        {**_identity(project), "objective": "Exercise the closure contract."}
    )["structuredContent"]["dag_id"]


def _add(server, project, dag_id, kind, summary, **extra):
    return server.tool_dag_add_atom(
        {
            "dag_id": dag_id,
            "project_path": project,
            "kind": kind,
            "summary": summary,
            "content": summary,
            **extra,
        }
    )["structuredContent"]["atom"]


def _finish(server, project, **extra):
    return server.tool_dag_finish_session({**_identity(project), **extra})[
        "structuredContent"
    ]


def _export_lint(server, project, dag_id):
    exported = server.tool_dag_export(
        {"dag_id": dag_id, "project_path": project, "format": "xml"}
    )["content"][0]["text"]
    return server.tool_lint_snippet({"snippet": exported})["structuredContent"]


# --- metadata list/read parity (the nine positive review probes) ------------

@pytest.mark.parametrize(
    "field,expected",
    [
        ("tags", PROBE_TAGS),
        ("model", "probe-model"),
        ("orchestrator", "probe-orchestrator"),
    ],
)
def test_listing_preserves_tags_and_tag_derived_provenance(server, project, field, expected):
    started = server.tool_dag_start(
        {
            "project_path": project,
            "title": "Metadata fixture",
            "objective": "Check metadata preservation.",
            "tags": PROBE_TAGS,
        }
    )["structuredContent"]
    listed = server.tool_dag_list({"project_path": project, "limit": 1})[
        "structuredContent"
    ]["dags"][0]
    read = server.tool_dag_read(
        {"project_path": project, "dag_id": started["dag_id"]}
    )["structuredContent"]["dag"]
    assert listed.get(field) == expected
    assert read.get(field) == expected


def test_stored_columns_keep_precedence_over_tag_fallbacks(server, project):
    dag_id = server.tool_dag_start(
        {
            "project_path": project,
            "title": "Precedence fixture",
            "objective": "Column beats tag.",
            "tags": PROBE_TAGS,
        }
    )["structuredContent"]["dag_id"]
    server.tool_dag_set_model(
        {"dag_id": dag_id, "project_path": project, "model": "stored-model"}
    )
    listed = server.tool_dag_list({"project_path": project, "limit": 1})[
        "structuredContent"
    ]["dags"][0]
    assert listed["model"] == "stored-model"
    assert listed["tags"] == PROBE_TAGS


def test_listing_counts_ordering_filter_and_limit_survive_normalization(server, project, tmp_path, monkeypatch):
    # now() truncates to whole seconds, so same-second creations tie on
    # updated_at and make the ordering assertion flaky; use a strictly
    # increasing clock.
    ticks = iter(range(10_000))

    def fake_now():
        tick = next(ticks)
        return f"2026-01-01T{tick // 3600:02d}:{tick // 60 % 60:02d}:{tick % 60:02d}Z"

    monkeypatch.setattr(server, "now", fake_now)
    first = server.tool_dag_start(
        {"project_path": project, "title": "older", "objective": "o", "tags": ["one"]}
    )["structuredContent"]["dag_id"]
    _add(server, project, first, "Observation", "Node for counting.")
    second = server.tool_dag_start(
        {"project_path": project, "title": "newer", "objective": "o", "tags": ["two"]}
    )["structuredContent"]["dag_id"]
    other_project = str(tmp_path / "elsewhere")
    server.tool_dag_start(
        {"project_path": other_project, "title": "foreign", "objective": "o"}
    )
    listed = server.tool_dag_list({"project_path": project, "limit": 10})[
        "structuredContent"
    ]["dags"]
    assert [dag["dag_id"] for dag in listed] == [second, first]
    by_id = {dag["dag_id"]: dag for dag in listed}
    # dag_start seeds a Goal atom, so counts are goal + explicit additions.
    assert by_id[first]["node_count"] == 2
    assert by_id[second]["node_count"] == 1
    assert len(
        server.tool_dag_list({"project_path": project, "limit": 1})["structuredContent"]["dags"]
    ) == 1


# --- premise-free explicit closure: reject with zero mutation ---------------

@pytest.mark.parametrize("outcome", OUTCOMES)
def test_premise_free_closure_rejects_without_any_mutation(server, project, outcome):
    dag_id = _ensure(server, project)
    before = _snapshot(server)
    with pytest.raises(ValueError, match="refer_at_least_one"):
        _finish(server, project, outcome=outcome, summary="Premature closure.")
    assert _snapshot(server) == before
    dag = server.load_dag(dag_id, project)
    kinds = sorted(node["kind"] for node in dag["nodes"].values())
    assert kinds == ["Goal"], "no Concluding persisted, no premise fabricated"
    # The session stays bound: the same identity resumes the same DAG.
    resumed = server.tool_dag_ensure_session(_identity(project))["structuredContent"]
    assert resumed["created"] is False and resumed["dag_id"] == dag_id


# --- supported closure: real premise, explicit status, clean lint -----------

@pytest.mark.parametrize("outcome", OUTCOMES)
def test_supported_closure_cites_premise_and_lints(server, project, outcome):
    dag_id = _ensure(server, project)
    premise = _add(
        server, project, dag_id, "Observation", f"Fixture observed the outcome: {outcome}."
    )
    finished = _finish(server, project, outcome=outcome, summary=f"Outcome: {outcome}.")
    atom = finished["atom"]
    assert atom["kind"] == "Concluding"
    assert atom["attributes"]["status"] == outcome
    assert atom["attributes"]["for_goal"]
    dag = server.load_dag(dag_id, project)
    assert any(
        edge["from"] == atom["id"] and edge["to"] == premise["id"] for edge in dag["edges"]
    )
    lint = _export_lint(server, project, dag_id)
    assert lint["ok"] is True, lint.get("errors")


# --- lifecycle and invalid combinations -------------------------------------

def test_no_outcome_finish_stays_a_lifecycle_observation(server, project):
    _ensure(server, project)
    finished = _finish(server, project, summary="Session ended without a verdict.")
    assert finished["found"] is True
    assert finished["atom"]["kind"] == "Observation"
    assert finished["outcome"] is None


@pytest.mark.parametrize(
    "extra",
    [
        pytest.param({"outcome": "invented"}, id="invalid_outcome"),
        pytest.param({"kind": "Observation", "outcome": "met"}, id="invalid_kind_outcome"),
    ],
)
def test_invalid_finish_combinations_reject_without_mutation(server, project, extra):
    _ensure(server, project)
    before = _snapshot(server)
    with pytest.raises(ValueError):
        _finish(server, project, **extra)
    assert _snapshot(server) == before


# --- adversarial: concurrency, rollback, opt-out/resume ---------------------

def test_concurrent_atom_creation_yields_unique_ids_and_ordinals(server, project):
    dag_id = server.tool_dag_start(
        {"project_path": project, "title": "Concurrency", "objective": "o"}
    )["structuredContent"]["dag_id"]
    writers = 8
    barrier = threading.Barrier(writers)
    failures = []

    def write(index):
        barrier.wait()
        try:
            _add(server, project, dag_id, "Finding", f"Concurrent finding {index}.")
        except Exception as exc:  # noqa: BLE001 - collected for the assertion
            failures.append(exc)

    threads = [threading.Thread(target=write, args=(i,)) for i in range(writers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert failures == []
    with sqlite3.connect(server.database_path()) as conn:
        rows = conn.execute(
            "SELECT atom_id, ordinal FROM nodes WHERE dag_id = ?", (dag_id,)
        ).fetchall()
    finding_ids = [row[0] for row in rows if row[0].startswith("Finding_")]
    ordinals = [row[1] for row in rows]
    assert len(finding_ids) == writers
    assert len(set(finding_ids)) == writers
    # dag_start's seed Goal holds ordinal 1; the racing writers must still
    # produce a dense, collision-free ordinal sequence after it.
    assert sorted(ordinals) == list(range(1, writers + 2))


def test_concurrent_opposite_links_cannot_persist_a_cycle(server, project):
    dag_id = server.tool_dag_start(
        {"project_path": project, "title": "Cycle race", "objective": "o"}
    )["structuredContent"]["dag_id"]
    a = _add(server, project, dag_id, "Finding", "First endpoint.")["id"]
    b = _add(server, project, dag_id, "Finding", "Second endpoint.")["id"]
    barrier = threading.Barrier(2)
    outcomes = {}

    def link(name, from_id, to_id):
        barrier.wait()
        try:
            server.tool_dag_link(
                {"dag_id": dag_id, "project_path": project, "from": from_id, "to": to_id}
            )
            outcomes[name] = "ok"
        except ValueError as exc:
            outcomes[name] = str(exc)

    threads = [
        threading.Thread(target=link, args=("forward", a, b)),
        threading.Thread(target=link, args=("backward", b, a)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(v == "ok" for v in outcomes.values()) == [False, True], outcomes
    edges = {(edge["from"], edge["to"]) for edge in server.load_dag(dag_id, project)["edges"]}
    assert not ((a, b) in edges and (b, a) in edges)


def test_rejected_atom_mutation_rolls_back_nodes_edges_and_counters(server, project):
    dag_id = server.tool_dag_start(
        {"project_path": project, "title": "Rollback", "objective": "o"}
    )["structuredContent"]["dag_id"]
    _add(server, project, dag_id, "Finding", "Existing atom.")
    before = _snapshot(server)
    with pytest.raises(ValueError, match="unknown to atom"):
        _add(
            server,
            project,
            dag_id,
            "Finding",
            "Doomed atom.",
            links=[{"to": "Ghost_9999", "relation": "refers"}],
        )
    assert _snapshot(server) == before
    follow_up = _add(server, project, dag_id, "Finding", "Counter is unscathed.")
    assert follow_up["id"] == "Finding_0002"


def test_live_opt_out_gates_auto_sessions_but_not_explicit_ones(server, project, monkeypatch):
    monkeypatch.setenv("SCHOLIA_AUTOEMIT", "0")
    gated = server.tool_dag_ensure_session(
        {"project_path": project, "host": "contract-host", "auto": True}
    )["structuredContent"]
    assert gated["enabled"] is False and gated["created"] is False
    assert server.tool_dag_list({"project_path": project, "limit": 10})[
        "structuredContent"
    ]["dags"] == []
    explicit = server.tool_dag_ensure_session(_identity(project))["structuredContent"]
    assert explicit["created"] is True


def test_resume_backfills_model_and_orchestrator_first_writer_wins(server, project):
    dag_id = _ensure(server, project)
    resumed = server.tool_dag_ensure_session(
        {**_identity(project), "model": "late-model", "orchestrator": "late-orchestrator"}
    )["structuredContent"]
    assert resumed["dag_id"] == dag_id
    assert resumed["model"] == "late-model"
    assert resumed["orchestrator"] == "late-orchestrator"
    again = server.tool_dag_ensure_session(
        {**_identity(project), "model": "usurper", "orchestrator": "usurper"}
    )["structuredContent"]
    assert again["model"] == "late-model"
    assert again["orchestrator"] == "late-orchestrator"
