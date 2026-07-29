"""MCP 2026-07-28 dual-version conformance (PRD-01).

Exercises the wire adapter over real STDIO: the 2026-07-28 surface
(``server/discover``, per-request ``_meta`` version carriage, ``resultType``,
CacheableResult ``ttlMs``/``cacheScope``, fail-closed on unsupported versions)
plus continued backward compatibility with the pre-2026 ``initialize``/``ping``
handshake.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from scholialang_mcp.server import (
    META_PROTOCOL_VERSION,
    META_SERVER_INFO,
    SUPPORTED_MCP_PROTOCOL_VERSIONS,
    UNSUPPORTED_PROTOCOL_VERSION,
)

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "atlas" / "sample"


def _env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    return env


def _rpc(proc: "subprocess.Popen[str]", payload: dict[str, object]) -> dict[str, object]:
    assert proc.stdin is not None and proc.stdout is not None
    proc.stdin.write(json.dumps(payload) + "\n")
    proc.stdin.flush()
    line = proc.stdout.readline()
    assert line
    return json.loads(line)


def _server() -> "subprocess.Popen[str]":
    return subprocess.Popen(
        [sys.executable, "-m", "scholialang_mcp", "--repo-root", str(FIXTURE)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_env(),
    )


def test_2026_release_is_supported_and_preferred() -> None:
    assert "2026-07-28" in SUPPORTED_MCP_PROTOCOL_VERSIONS
    assert SUPPORTED_MCP_PROTOCOL_VERSIONS[0] == "2026-07-28"


def test_server_discover_advertises_versions_and_identity() -> None:
    proc = _server()
    try:
        r = _rpc(proc, {"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {}})
        res = r["result"]
        assert "2026-07-28" in res["protocolVersions"]
        assert res["serverInfo"]["name"] == "mcp__scholialang__atlas"
        assert res["resultType"] == "complete"
        assert META_SERVER_INFO in res["_meta"]
    finally:
        proc.terminate()


def test_stateless_request_reads_version_from_meta_and_lists_cacheable() -> None:
    proc = _server()
    try:
        r = _rpc(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
                "params": {"_meta": {META_PROTOCOL_VERSION: "2026-07-28"}},
            },
        )
        res = r["result"]
        assert isinstance(res["tools"], list) and res["tools"]
        assert res["resultType"] == "complete"
        assert isinstance(res["ttlMs"], int) and res["ttlMs"] > 0
        assert res["cacheScope"] in ("public", "private")
        assert META_SERVER_INFO in res["_meta"]
    finally:
        proc.terminate()


def test_unsupported_version_fails_closed() -> None:
    proc = _server()
    try:
        r = _rpc(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/list",
                "params": {"_meta": {META_PROTOCOL_VERSION: "1999-01-01"}},
            },
        )
        assert r["error"]["code"] == UNSUPPORTED_PROTOCOL_VERSION
    finally:
        proc.terminate()


def test_legacy_initialize_and_ping_still_work() -> None:
    proc = _server()
    try:
        init = _rpc(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "initialize",
                "params": {"protocolVersion": "2025-11-25", "capabilities": {}},
            },
        )
        assert init["result"]["protocolVersion"] == "2025-11-25"
        assert init["result"]["serverInfo"]["name"] == "mcp__scholialang__atlas"
        pong = _rpc(proc, {"jsonrpc": "2.0", "id": 5, "method": "ping", "params": {}})
        assert "result" in pong
    finally:
        proc.terminate()


def test_initialize_negotiates_up_to_2026_when_offered() -> None:
    proc = _server()
    try:
        init = _rpc(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "initialize",
                "params": {"protocolVersion": "2026-07-28", "capabilities": {}},
            },
        )
        assert init["result"]["protocolVersion"] == "2026-07-28"
    finally:
        proc.terminate()
