"""Real SQLite persistence, deterministic crash boundaries, and bounded state."""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from scholialang_mcp.durable_store import DurableCapabilityStore, Retention, Scope, StoreError


SCOPE = Scope("owner-a", "project-a")


def store(path, **kwargs):
    return DurableCapabilityStore(path, SCOPE, enabled=True, **kwargs)


def put(db, key="task", token="request", **kwargs):
    return db.put("tasks", key, {"status": "working"}, idempotency_key=token, **kwargs)


def child(path: Path, script: str, *args: str):
    return subprocess.run(
        [sys.executable, "-c", script, str(path), *args],
        capture_output=True, text=True, timeout=7, env=os.environ.copy(),
    )


CHILD_SETUP = """
import json, os, sys
from pathlib import Path
from scholialang_mcp.durable_store import DurableCapabilityStore, Scope
path = Path(sys.argv[1])
scope = Scope('owner-a', 'project-a')
"""


def test_default_off_has_no_disk_effect(tmp_path):
    path = tmp_path / "missing" / "state.db"
    with pytest.raises(StoreError, match="disabled_by_policy"):
        DurableCapabilityStore(path, SCOPE)
    assert not path.parent.exists()


@pytest.mark.parametrize("scope", [Scope("owner-b", "project-a"), Scope("owner-a", "project-b")])
def test_foreign_scope_cannot_reopen(tmp_path, scope):
    path = tmp_path / "state.db"
    put(store(path))
    with pytest.raises(StoreError, match="scope_denied"):
        DurableCapabilityStore(path, scope, enabled=True)
    assert len(store(path).refetch()["records"]) == 1


@pytest.mark.parametrize("stage", ["after_state", "before_commit"])
def test_precommit_fault_rolls_back_state_intent_and_receipt(tmp_path, stage):
    path = tmp_path / "state.db"
    db = store(path)
    first = put(db)
    observations = []

    def fault(point):
        if point == stage:
            # A separate connection sees the old committed snapshot during the
            # open writer transaction, not the candidate state or intent.
            observations.append(db.refetch())
            assert [e["revision"] for e in db.events()] == [1]
            raise RuntimeError(stage)

    broken = store(path, fault=fault)
    with pytest.raises(RuntimeError, match=stage):
        broken.put("tasks", "task", {"status": "completed"}, idempotency_key="update")
    assert observations[0]["records"] == [first]
    assert store(path).refetch()["records"] == [first]
    assert put(store(path), token="update")["revision"] == 2


@pytest.mark.parametrize("stage", ["after_state", "before_commit", "after_commit", "before_notify", "after_notify"])
def test_abrupt_process_exit_then_independent_restart(tmp_path, stage):
    path = tmp_path / "state.db"
    script = CHILD_SETUP + """
stage = sys.argv[2]
def fault(point):
    if point == stage:
        os._exit(73)
db = DurableCapabilityStore(path, scope, enabled=True, fault=fault)
db.put('tasks', 'task', {'status': 'working'}, idempotency_key='request', expected_revision=0)
def notify(event):
    with open(str(path) + '.notifications', 'a') as f:
        f.write(json.dumps(event) + '\\n')
        f.flush()
        os.fsync(f.fileno())
db.dispatch(notify)
"""
    crashed = child(path, script, stage)
    assert crashed.returncode == 73, crashed.stderr
    # This new interpreter imports the on-disk backend independently. It also
    # examines the tables with sqlite3, rather than trusting only its API.
    restarted = child(path, CHILD_SETUP + """
import sqlite3
db = DurableCapabilityStore(path, scope, enabled=True)
with sqlite3.connect(path) as conn:
    counts = [conn.execute('SELECT COUNT(*) FROM ' + t).fetchone()[0]
              for t in ('records', 'outbox', 'receipts')]
    integrity = conn.execute('PRAGMA integrity_check').fetchone()[0]
print(json.dumps({'snapshot': db.refetch(), 'events': db.events(), 'counts': counts, 'integrity': integrity}))
""")
    assert restarted.returncode == 0, restarted.stderr
    evidence = json.loads(restarted.stdout)
    committed = stage not in {"after_state", "before_commit"}
    assert evidence["counts"] == [int(committed)] * 3
    assert evidence["snapshot"]["revision"] == int(committed)
    assert len(evidence["snapshot"]["records"]) == int(committed)
    assert evidence["integrity"] == "ok"
    notification_path = Path(str(path) + ".notifications")
    assert notification_path.exists() == (stage == "after_notify")
    db = store(path)
    # Response-loss retry returns the original receipt despite create-only CAS.
    result = put(db, expected_revision=0)
    assert result["revision"] == 1
    notifications = []
    assert db.dispatch(notifications.append) == 1
    assert notifications[0]["revision"] == 1
    assert db.dispatch(notifications.append) == 0
    print(json.dumps({"crash_stage": stage, "committed": committed,
                      "sqlite_counts": evidence["counts"], "integrity": evidence["integrity"],
                      "restart_revision": result["revision"], "retry_notifications": len(notifications)}))


def test_notification_observes_committed_state_and_failure_is_retryable(tmp_path):
    path = tmp_path / "state.db"
    db = store(path)
    put(db)

    def notify(event):
        assert store(path).refetch()["revision"] == event["revision"]
        raise OSError("delivery failed")

    with pytest.raises(OSError, match="delivery failed"):
        db.dispatch(notify)
    assert db.events()[0]["delivered"] == 0
    seen = []
    assert store(path).dispatch(seen.append) == 1
    assert seen[0]["revision"] == 1


def test_concurrent_processes_compare_and_swap(tmp_path):
    path = tmp_path / "state.db"
    put(store(path))
    script = CHILD_SETUP + """
from scholialang_mcp.durable_store import StoreError
db = DurableCapabilityStore(path, scope, enabled=True)
try:
    result = db.put('tasks', 'task', {'writer': sys.argv[2]},
                    idempotency_key=sys.argv[2], expected_revision=1)
    print(result['revision'])
except StoreError as exc:
    print(exc.code)
"""
    processes = [subprocess.Popen([sys.executable, "-c", script, str(path), str(i)],
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                 for i in range(2)]
    try:
        results = [p.communicate(timeout=7) for p in processes]
        assert all(p.returncode == 0 for p in processes), results
        assert sorted(out.strip() for out, err in results) == ["2", "revision_conflict"]
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.wait()
    assert store(path).refetch()["revision"] == 2
    assert [e["revision"] for e in store(path).events()] == [1, 2]


def test_expiry_reconnect_cursor_and_clock_rollback(tmp_path):
    path = tmp_path / "state.db"
    now = [100.0]
    policy = Retention(state_ttl_ms=1000, outbox_ttl_ms=500, receipt_ttl_ms=300)
    db = store(path, retention=policy, clock=lambda: now[0])
    put(db)
    now[0] = 100.499
    assert len(db.events()) == 1
    now[0] = 100.5
    with pytest.raises(StoreError, match="resync_required"):
        db.events()
    assert db.events(after_revision=1) == []
    assert len(db.refetch()["records"]) == 1
    now[0] = 101.0
    assert db.refetch()["records"] == []
    assert db.prune() == {"records": 0, "outbox": 0, "receipts": 0}
    reopened = store(path, retention=policy, clock=lambda: 99.0)
    assert reopened.refetch()["records"] == []
    assert put(reopened)["revision"] == 2
    assert reopened.refetch()["records"][0]["expires_ms"] == 102000


def test_count_bounds_and_explicit_resync(tmp_path):
    path = tmp_path / "state.db"
    policy = Retention(max_records=1, max_events=2, max_receipts=3)
    db = store(path, retention=policy)
    put(db)
    with pytest.raises(StoreError, match="record_limit"):
        put(db, key="other", token="second")
    put(db, token="second")
    put(db, token="third")
    assert db.prune() == {"records": 1, "outbox": 2, "receipts": 3}
    with pytest.raises(StoreError, match="resync_required"):
        db.events()
    assert [e["revision"] for e in db.events(after_revision=1)] == [2, 3]
    with pytest.raises(StoreError, match="receipt_limit"):
        put(db, token="fourth")
    assert db.refetch()["revision"] == 3
    assert put(db)["revision"] == 1


def test_receipt_expiry_and_idle_dispatch_expiry(tmp_path):
    now = [100.0]
    policy = Retention(state_ttl_ms=1000, outbox_ttl_ms=500, receipt_ttl_ms=300)
    db = store(tmp_path / "state.db", retention=policy, clock=lambda: now[0])
    assert put(db)["revision"] == 1
    now[0] = 100.299
    assert put(db)["revision"] == 1
    now[0] = 100.3
    assert put(db)["revision"] == 2
    now[0] = 100.8
    seen = []
    assert db.dispatch(seen.append) == 0
    assert seen == []
    with pytest.raises(StoreError, match="resync_required"):
        db.events(after_revision=1)
    assert db.refetch()["records"][0]["revision"] == 2


def test_native_sqlite_full_is_atomic(tmp_path):
    path = tmp_path / "state.db"
    db = store(path, retention=Retention(max_database_bytes=65536, max_payload_bytes=60000))
    first = put(db)
    with pytest.raises(sqlite3.OperationalError, match="full"):
        db.put("tasks", "large", {"data": "x" * 55000}, idempotency_key="large")
    assert db.refetch()["records"] == [first]
    assert len(db.events()) == 1
    assert db.prune()["receipts"] == 1


def test_native_commit_lock_failure_rolls_back(tmp_path):
    path = tmp_path / "state.db"
    first = put(store(path))
    reader = sqlite3.connect(path, isolation_level=None)

    def fault(stage):
        if stage == "before_commit":
            reader.execute("BEGIN")
            assert reader.execute("SELECT revision FROM records").fetchone()[0] == 1

    try:
        with pytest.raises(sqlite3.OperationalError, match="locked"):
            put(store(path, fault=fault), token="second")
    finally:
        reader.close()
    db = store(path)
    assert db.refetch()["records"] == [first]
    assert [event["revision"] for event in db.events()] == [1]
    assert put(db, token="second")["revision"] == 2


def test_retention_churn_has_bounded_physical_size(tmp_path):
    path = tmp_path / "state.db"
    now = [100.0]
    policy = Retention(state_ttl_ms=10, outbox_ttl_ms=10, receipt_ttl_ms=10,
                       max_records=1, max_events=1, max_receipts=1,
                       max_database_bytes=131072)
    db = store(path, retention=policy, clock=lambda: now[0])
    sizes = []
    for i in range(100):
        db.put("tasks", str(i), {"data": "x" * 1000}, idempotency_key=str(i))
        sizes.append(path.stat().st_size)
        now[0] += 1
    assert max(sizes) <= policy.max_database_bytes
    assert len(set(sizes[10:])) == 1
    assert db.prune() == {"records": 0, "outbox": 0, "receipts": 0}
    assert not Path(str(path) + "-journal").exists()
    print(json.dumps({"retention_writes": 100, "peak_database_bytes": max(sizes),
                      "steady_database_bytes": sizes[-1], "configured_limit_bytes": policy.max_database_bytes}))


def test_idempotency_conflict_and_detached_payload(tmp_path):
    db = store(tmp_path / "state.db")
    result = put(db)
    result["payload"]["status"] = "changed"
    assert put(db)["payload"] == {"status": "working"}
    with pytest.raises(StoreError, match="idempotency_conflict"):
        put(db, key="other")


@pytest.mark.parametrize("kwargs,code", [
    ({"payload": {"bad": float("nan")}}, "invalid_payload"),
    ({"payload": []}, "invalid_payload"),
    ({"payload": {1: "non-string key"}}, "invalid_payload"),
    ({"payload": {"tuple": (1, 2)}}, "invalid_payload"),
    ({"payload": {"data": "x" * 16384}}, "payload_too_large"),
    ({"ttl_ms": True}, "invalid_ttl"),
    ({"ttl_ms": 0}, "invalid_ttl"),
    ({"ttl_ms": 86400001}, "invalid_ttl"),
    ({"expected_revision": True}, "invalid_revision"),
    ({"key": ""}, "invalid_identifier"),
])
def test_invalid_inputs_have_no_state_effect(tmp_path, kwargs, code):
    db = store(tmp_path / "state.db")
    args = {"kind": "tasks", "key": "task", "payload": {}, "idempotency_key": "request", **kwargs}
    with pytest.raises(StoreError, match=code):
        db.put(**args)
    assert db.refetch()["revision"] == 0
    assert db.events() == []


def test_policy_drift_and_unknown_schema_fail_closed(tmp_path):
    path = tmp_path / "state.db"
    put(store(path))
    with pytest.raises(StoreError, match="retention_mismatch"):
        store(path, retention=replace(Retention(), max_events=2))
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA user_version=99")
    with pytest.raises(StoreError, match="unsupported_store_schema"):
        store(path)
