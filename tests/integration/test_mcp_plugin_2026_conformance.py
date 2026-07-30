"""MCP 2026-07-28 dual-version conformance for the plugin vendored server (PRD-01).

Same battery as ``test_mcp_2026_conformance.py`` but against
``plugins/claude-code/scholialang/scripts/scholialang_mcp_server.py`` — the
canonical plugin copy — plus the resources surface it alone exposes and the
byte-parity gate that keeps the Codex and Ollama copies generated from it.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CANONICAL = ROOT / "plugins" / "claude-code" / "scholialang" / "scripts" / "scholialang_mcp_server.py"
GENERATED = (
    ROOT / "plugins" / "codex" / "scholialang" / "scripts" / "scholialang_mcp_server.py",
    ROOT / "plugins" / "ollama" / "scholialang" / "scripts" / "scholialang_mcp_server.py",
)

META_PROTOCOL_VERSION = "io.modelcontextprotocol/protocolVersion"
META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"
UNSUPPORTED_PROTOCOL_VERSION = -32022


def _rpc(proc: "subprocess.Popen[str]", payload: dict[str, object]) -> dict[str, object]:
    assert proc.stdin is not None and proc.stdout is not None
    proc.stdin.write(json.dumps(payload) + "\n")
    proc.stdin.flush()
    line = proc.stdout.readline()
    assert line
    return json.loads(line)


def _server(tmp_path: Path) -> "subprocess.Popen[str]":
    env = os.environ.copy()
    # Keep the DAG store out of the operator's real state.
    env["SCHOLIALANG_HOME"] = str(tmp_path)
    return subprocess.Popen(
        [sys.executable, str(CANONICAL)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        cwd=str(tmp_path),
    )


def test_plugin_copies_are_byte_identical_to_canonical() -> None:
    canonical = hashlib.sha256(CANONICAL.read_bytes()).hexdigest()
    for copy in GENERATED:
        assert hashlib.sha256(copy.read_bytes()).hexdigest() == canonical, (
            f"{copy} drifted from the canonical claude-code server; "
            "run scripts/sync_plugins.sh"
        )


def test_server_discover_advertises_versions_and_identity(tmp_path: Path) -> None:
    proc = _server(tmp_path)
    try:
        r = _rpc(proc, {"jsonrpc": "2.0", "id": 1, "method": "server/discover", "params": {}})
        res = r["result"]
        assert res["protocolVersions"][0] == "2026-07-28"
        assert res["serverInfo"]["name"] == "scholialang"
        assert res["resultType"] == "complete"
        assert META_SERVER_INFO in res["_meta"]
    finally:
        proc.terminate()


def test_stateless_list_read_results_are_cacheable_and_private(tmp_path: Path) -> None:
    proc = _server(tmp_path)
    try:
        listed = _rpc(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/list",
                "params": {"_meta": {META_PROTOCOL_VERSION: "2026-07-28"}},
            },
        )
        res = listed["result"]
        assert isinstance(res["tools"], list) and res["tools"]
        assert res["resultType"] == "complete"
        assert isinstance(res["ttlMs"], int) and res["ttlMs"] > 0
        assert res["cacheScope"] == "private"

        resources = _rpc(proc, {"jsonrpc": "2.0", "id": 3, "method": "resources/list", "params": {}})
        assert resources["result"]["cacheScope"] == "private"
        assert isinstance(resources["result"]["ttlMs"], int)
        uri = resources["result"]["resources"][0]["uri"]

        read = _rpc(
            proc,
            {"jsonrpc": "2.0", "id": 4, "method": "resources/read", "params": {"uri": uri}},
        )
        assert read["result"]["cacheScope"] == "private"
        assert isinstance(read["result"]["ttlMs"], int)
        assert read["result"]["resultType"] == "complete"

        templates = _rpc(
            proc,
            {"jsonrpc": "2.0", "id": 5, "method": "resources/templates/list", "params": {}},
        )
        assert templates["result"]["cacheScope"] == "private"
    finally:
        proc.terminate()


def test_tools_list_ordering_is_deterministic(tmp_path: Path) -> None:
    proc = _server(tmp_path)
    try:
        first = _rpc(proc, {"jsonrpc": "2.0", "id": 6, "method": "tools/list", "params": {}})
        second = _rpc(proc, {"jsonrpc": "2.0", "id": 7, "method": "tools/list", "params": {}})
        names = [t["name"] for t in first["result"]["tools"]]
        assert names == [t["name"] for t in second["result"]["tools"]]
        assert len(names) == len(set(names))
    finally:
        proc.terminate()


def test_unsupported_version_fails_closed(tmp_path: Path) -> None:
    proc = _server(tmp_path)
    try:
        stateless = _rpc(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 8,
                "method": "tools/list",
                "params": {"_meta": {META_PROTOCOL_VERSION: "1999-01-01"}},
            },
        )
        assert stateless["error"]["code"] == UNSUPPORTED_PROTOCOL_VERSION
        legacy = _rpc(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 9,
                "method": "initialize",
                "params": {"protocolVersion": "2023-01-01", "capabilities": {}},
            },
        )
        assert legacy["error"]["code"] == UNSUPPORTED_PROTOCOL_VERSION
    finally:
        proc.terminate()


def test_legacy_initialize_and_ping_still_work(tmp_path: Path) -> None:
    proc = _server(tmp_path)
    try:
        init = _rpc(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 10,
                "method": "initialize",
                "params": {"protocolVersion": "2025-11-25", "capabilities": {}},
            },
        )
        assert init["result"]["protocolVersion"] == "2025-11-25"
        assert init["result"]["serverInfo"]["name"] == "scholialang"
        pong = _rpc(proc, {"jsonrpc": "2.0", "id": 11, "method": "ping", "params": {}})
        assert "result" in pong
    finally:
        proc.terminate()
