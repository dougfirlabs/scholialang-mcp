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
        # PRD mcp-2026-07-28-prd-01: cache scope is private — this server runs
        # against one operator's project, nothing may hit a shared cache.
        assert res["cacheScope"] == "private"
        assert META_SERVER_INFO in res["_meta"]
    finally:
        proc.terminate()


def test_tools_list_ordering_is_deterministic() -> None:
    proc = _server()
    try:
        first = _rpc(proc, {"jsonrpc": "2.0", "id": 20, "method": "tools/list", "params": {}})
        second = _rpc(proc, {"jsonrpc": "2.0", "id": 21, "method": "tools/list", "params": {}})
        names = [t["name"] for t in first["result"]["tools"]]
        assert names == [t["name"] for t in second["result"]["tools"]]
        assert len(names) == len(set(names))
    finally:
        proc.terminate()


def test_unknown_method_is_not_dispatched_as_tool() -> None:
    # 0.6.2 dispatched unknown JSON-RPC methods as direct tool invocations
    # (audit p7). That undocumented surface is removed: tools are reachable
    # only through tools/call.
    proc = _server()
    try:
        r = _rpc(proc, {"jsonrpc": "2.0", "id": 22, "method": "get_tree", "params": {}})
        assert r["error"]["code"] == -32601
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


def test_mixed_version_request_cannot_silently_mis_negotiate() -> None:
    """An unsupported version anywhere in the request fails closed.

    Legacy negotiation allowed a server to counter-offer its preferred
    version; the PRD forbids that silent fallback. ``_meta`` (the 2026-07-28
    carrier) takes precedence over legacy ``params.protocolVersion``, so a
    request declaring an unsupported ``_meta`` version fails even when the
    legacy field carries a supported one.
    """
    proc = _server()
    try:
        legacy_init = _rpc(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 30,
                "method": "initialize",
                "params": {"protocolVersion": "2023-01-01", "capabilities": {}},
            },
        )
        assert legacy_init["error"]["code"] == UNSUPPORTED_PROTOCOL_VERSION
        mixed = _rpc(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 31,
                "method": "tools/list",
                "params": {
                    "_meta": {META_PROTOCOL_VERSION: "1999-01-01"},
                    "protocolVersion": "2025-11-25",
                },
            },
        )
        assert mixed["error"]["code"] == UNSUPPORTED_PROTOCOL_VERSION
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
