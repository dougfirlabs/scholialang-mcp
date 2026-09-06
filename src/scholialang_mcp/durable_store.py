"""Opt-in, scope-bound capability state and notification outbox.

This is an application storage seam, not an MCP wire adapter. Every operation
opens its own SQLite connection; no process-local object is authoritative.
See docs/durable-capability-store.md for delivery and retention guarantees.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterator


class StoreError(ValueError):
    """Stable application error; adapters map ``code`` to their own wire era."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _identifier(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.encode()) > 256:
        raise StoreError("invalid_identifier")
    return value


def _json(value: Any) -> str:
    def check(item: Any) -> None:
        if isinstance(item, dict):
            if any(not isinstance(key, str) for key in item):
                raise ValueError("JSON object keys must be strings")
            for child in item.values():
                check(child)
        elif isinstance(item, list):
            for child in item:
                check(child)
        elif item is not None and type(item) not in (str, bool, int, float):
            raise ValueError("payload must contain only JSON types")

    try:
        check(value)
        return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        raise StoreError("invalid_payload") from exc


@dataclass(frozen=True)
class Scope:
    owner: str
    project: str

    def __post_init__(self) -> None:
        _identifier(self.owner)
        _identifier(self.project)


@dataclass(frozen=True)
class Retention:
    state_ttl_ms: int = 86_400_000
    outbox_ttl_ms: int = 3_600_000
    receipt_ttl_ms: int = 3_600_000
    max_records: int = 256
    max_events: int = 512
    max_receipts: int = 512
    max_payload_bytes: int = 16_384
    max_database_bytes: int = 67_108_864

    def __post_init__(self) -> None:
        if any(type(v) is not int or v <= 0 for v in asdict(self).values()):
            raise StoreError("invalid_retention")
        if self.max_database_bytes < 65_536:
            raise StoreError("invalid_retention")


class DurableCapabilityStore:
    """One local database per trusted owner/project; disabled by default.

    The host chooses the path and authenticated scope, never request arguments.
    Fault hooks are local test instrumentation and are not exposed over MCP.
    """

    def __init__(
        self, path: Path, scope: Scope, *, enabled: bool = False,
        retention: Retention = Retention(),
        clock: Callable[[], float] = time.time,
        fault: Callable[[str], None] | None = None,
    ):
        if enabled is not True:
            raise StoreError("disabled_by_policy")
        self.path = Path(path).resolve()
        self.scope = scope
        self.retention = retention
        self.clock = clock
        self.fault = fault or (lambda stage: None)
        # The parent is deliberately host-provisioned; no global/default DB.
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            if version not in (0, 1) or (version == 0 and tables):
                raise StoreError("unsupported_store_schema")
            if version == 0:
                for statement in (
                    "CREATE TABLE metadata (singleton INTEGER PRIMARY KEY CHECK(singleton=1), "
                    "owner TEXT NOT NULL, project TEXT NOT NULL, policy TEXT NOT NULL, "
                    "revision INTEGER NOT NULL, floor INTEGER NOT NULL, clock_ms INTEGER NOT NULL)",
                    "CREATE TABLE records (kind TEXT NOT NULL, key TEXT NOT NULL, "
                    "revision INTEGER NOT NULL, payload TEXT NOT NULL, expires_ms INTEGER NOT NULL, "
                    "PRIMARY KEY(kind, key))",
                    "CREATE TABLE outbox (revision INTEGER PRIMARY KEY, kind TEXT NOT NULL, "
                    "key TEXT NOT NULL, digest TEXT NOT NULL, expires_ms INTEGER NOT NULL, "
                    "delivered INTEGER NOT NULL DEFAULT 0)",
                    "CREATE TABLE receipts (token TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, "
                    "response TEXT NOT NULL, expires_ms INTEGER NOT NULL)",
                ):
                    conn.execute(statement)
                conn.execute("INSERT INTO metadata VALUES (1, ?, ?, ?, 0, 0, 0)",
                             (scope.owner, scope.project, _json(asdict(retention))))
                conn.execute("PRAGMA user_version=1")
            self._metadata(conn)
            conn.commit()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            # DELETE avoids an unbounded WAL held by long-lived readers. EXTRA
            # also syncs the directory after rollback-journal deletion.
            if conn.execute("PRAGMA journal_mode").fetchone()[0] != "delete":
                raise StoreError("unsupported_journal_mode")
            conn.execute("PRAGMA synchronous=EXTRA")
            page_size = conn.execute("PRAGMA page_size").fetchone()[0]
            pages = self.retention.max_database_bytes // page_size
            actual = conn.execute(f"PRAGMA max_page_count={pages}").fetchone()[0]
            if actual > pages:
                raise StoreError("storage_limit")
            yield conn
        finally:
            # Closing an unfinished explicit transaction rolls it back, also
            # for BaseException and faults immediately before commit.
            conn.close()

    def _metadata(self, conn: sqlite3.Connection) -> sqlite3.Row:
        if conn.execute("PRAGMA user_version").fetchone()[0] != 1:
            raise StoreError("unsupported_store_schema")
        row = conn.execute("SELECT * FROM metadata WHERE singleton=1").fetchone()
        if row is None or (row["owner"], row["project"]) != (self.scope.owner, self.scope.project):
            raise StoreError("scope_denied")
        if row["policy"] != _json(asdict(self.retention)):
            raise StoreError("retention_mismatch")
        return row

    def _now(self, metadata: sqlite3.Row) -> int:
        stamp = self.clock()
        if not isinstance(stamp, (int, float)) or not math.isfinite(stamp) or stamp < 0:
            raise StoreError("invalid_clock")
        return max(int(stamp * 1000), metadata["clock_ms"])

    def _prune(self, conn: sqlite3.Connection, now: int) -> None:
        conn.execute("DELETE FROM records WHERE expires_ms<=?", (now,))
        conn.execute("DELETE FROM receipts WHERE expires_ms<=?", (now,))
        expired = conn.execute("SELECT MAX(revision) FROM outbox WHERE expires_ms<=?", (now,)).fetchone()[0]
        if expired is not None:
            # Remove a prefix, including when individual state TTLs differ.
            conn.execute("UPDATE metadata SET floor=MAX(floor, ?) WHERE singleton=1", (expired,))
            conn.execute("DELETE FROM outbox WHERE revision<=?", (expired,))
        conn.execute("UPDATE metadata SET clock_ms=? WHERE singleton=1", (now,))

    @staticmethod
    def _record(row: sqlite3.Row) -> dict[str, Any]:
        return {**dict(row), "payload": json.loads(row["payload"])}

    def put(
        self, kind: str, key: str, payload: dict[str, Any], *,
        idempotency_key: str, expected_revision: int | None = None,
        ttl_ms: int | None = None,
    ) -> dict[str, Any]:
        """Atomically commit one state replacement, intent, and retry receipt.

        expected_revision=0 means create-only. None permits unconditional writes.
        A response-loss retry must use the same request (including its precondition).
        """
        for identifier in (kind, key, idempotency_key):
            _identifier(identifier)
        if not isinstance(payload, dict):
            raise StoreError("invalid_payload")
        encoded = _json(payload)
        if len(encoded.encode()) > self.retention.max_payload_bytes:
            raise StoreError("payload_too_large")
        if expected_revision is not None and (type(expected_revision) is not int or expected_revision < 0):
            raise StoreError("invalid_revision")
        ttl = self.retention.state_ttl_ms if ttl_ms is None else ttl_ms
        if type(ttl) is not int or not 0 < ttl <= self.retention.state_ttl_ms:
            raise StoreError("invalid_ttl")
        fingerprint = hashlib.sha256(_json([kind, key, payload, expected_revision, ttl]).encode()).hexdigest()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            meta = self._metadata(conn)
            now = self._now(meta)
            self._prune(conn, now)
            receipt = conn.execute("SELECT * FROM receipts WHERE token=?", (idempotency_key,)).fetchone()
            if receipt:
                if receipt["fingerprint"] != fingerprint:
                    raise StoreError("idempotency_conflict")
                conn.commit()
                return json.loads(receipt["response"])
            old = conn.execute("SELECT revision FROM records WHERE kind=? AND key=?", (kind, key)).fetchone()
            if expected_revision is not None and expected_revision != (old[0] if old else 0):
                raise StoreError("revision_conflict")
            if old is None and conn.execute("SELECT COUNT(*) FROM records").fetchone()[0] >= self.retention.max_records:
                raise StoreError("record_limit")
            # Receipts cannot be silently evicted: that would break retry dedup.
            if conn.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] >= self.retention.max_receipts:
                raise StoreError("receipt_limit")
            revision = meta["revision"] + 1
            response = {"kind": kind, "key": key, "revision": revision,
                        "payload": json.loads(encoded), "expires_ms": now + ttl}
            conn.execute("INSERT OR REPLACE INTO records VALUES (?, ?, ?, ?, ?)",
                         (kind, key, revision, encoded, now + ttl))
            conn.execute("UPDATE metadata SET revision=? WHERE singleton=1", (revision,))
            self.fault("after_state")
            conn.execute("INSERT INTO outbox VALUES (?, ?, ?, ?, ?, 0)",
                         (revision, kind, key, hashlib.sha256(encoded.encode()).hexdigest(),
                          now + min(ttl, self.retention.outbox_ttl_ms)))
            conn.execute("INSERT INTO receipts VALUES (?, ?, ?, ?)",
                         (idempotency_key, fingerprint, _json(response),
                          now + min(ttl, self.retention.receipt_ttl_ms)))
            cutoff = revision - self.retention.max_events
            conn.execute("DELETE FROM outbox WHERE revision<=?", (cutoff,))
            conn.execute("UPDATE metadata SET floor=MAX(floor, ?) WHERE singleton=1", (cutoff,))
            self.fault("before_commit")
            conn.commit()
        self.fault("after_commit")
        return response

    def refetch(self) -> dict[str, Any]:
        """A reconnect snapshot: state and revision from one committed view."""
        with self._connect() as conn:
            conn.execute("BEGIN")
            meta = self._metadata(conn)
            now = self._now(meta)
            rows = conn.execute("SELECT * FROM records WHERE expires_ms>? ORDER BY kind, key", (now,))
            return {"scope": asdict(self.scope), "revision": meta["revision"],
                    "records": [self._record(row) for row in rows]}

    def events(self, *, after_revision: int = 0) -> list[dict[str, Any]]:
        """Internal bounded replay, not a standard MCP cursor/ack RPC."""
        if type(after_revision) is not int or after_revision < 0:
            raise StoreError("invalid_revision")
        with self._connect() as conn:
            conn.execute("BEGIN")
            meta = self._metadata(conn)
            now = self._now(meta)
            expired = conn.execute("SELECT MAX(revision) FROM outbox WHERE expires_ms<=?", (now,)).fetchone()[0]
            floor = max(meta["floor"], expired or 0)
            if after_revision < floor:
                raise StoreError("resync_required")
            if after_revision > meta["revision"]:
                raise StoreError("invalid_revision")
            return [dict(row) for row in conn.execute(
                "SELECT * FROM outbox WHERE revision>? AND expires_ms>? ORDER BY revision",
                (after_revision, now))]

    def dispatch(self, notify: Callable[[dict[str, Any]], None]) -> int:
        """Attempt committed pending intents in order; duplicates are possible.

        Each host should use one dispatcher. Concurrent dispatchers remain safe
        for state but can duplicate/reorder delivery. Refetch is authoritative.
        """
        with self._connect() as conn:
            conn.execute("BEGIN")
            now = self._now(self._metadata(conn))
            pending = [dict(row) for row in conn.execute(
                "SELECT * FROM outbox WHERE delivered=0 AND expires_ms>? ORDER BY revision", (now,))]
        sent = 0
        for event in pending:
            # A previous callback may have taken long enough for intent expiry.
            with self._connect() as conn:
                conn.execute("BEGIN")
                now = self._now(self._metadata(conn))
                live = conn.execute("SELECT 1 FROM outbox WHERE revision=? AND expires_ms>? AND delivered=0",
                                    (event["revision"], now)).fetchone()
            if live is None:
                continue
            self.fault("before_notify")
            notify(event)
            self.fault("after_notify")
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                self._metadata(conn)
                conn.execute("UPDATE outbox SET delivered=1 WHERE revision=?", (event["revision"],))
                conn.commit()
            sent += 1
        return sent

    def prune(self) -> dict[str, int]:
        """Persist expiry and its cursor floor; call periodically when idle."""
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._prune(conn, self._now(self._metadata(conn)))
            counts = {table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                      for table in ("records", "outbox", "receipts")}
            conn.commit()
            return counts
