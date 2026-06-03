from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from scholialang_mcp.server import codex_trace_config_snippet


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "atlas" / "sample"


def _env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    return env


def _rpc(proc: subprocess.Popen[str], payload: dict[str, object]) -> dict[str, object]:
    assert proc.stdin is not None
    assert proc.stdout is not None
    proc.stdin.write(json.dumps(payload) + "\n")
    proc.stdin.flush()
    line = proc.stdout.readline()
    assert line
    return json.loads(line)


def test_mcp_tools_list_and_file_lookup() -> None:
    proc = subprocess.Popen(
        [sys.executable, "-m", "scholialang_mcp", "--repo-root", str(FIXTURE)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=_env(),
    )
    try:
        init = _rpc(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "pytest", "version": "0"},
                },
            },
        )
        assert init["result"]["serverInfo"]["name"] == "mcp__scholialang__atlas"
        assert init["result"]["protocolVersion"] == "2025-06-18"

        listed = _rpc(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        tools = listed["result"]["tools"]
        assert len(tools) == 8
        assert {tool["name"] for tool in tools} >= {"lookup_file_summary", "get_tree"}

        called = _rpc(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "lookup_file_summary",
                    "arguments": {"path": "src/sample.py"},
                },
            },
        )
        text = called["result"]["content"][0]["text"]
        payload = json.loads(text)
        assert payload["source_path"].endswith("src/sample.py")
        assert payload["prose_preamble"] == "Small sample module exposing thing()."
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_disabled_mode_refuses_tools() -> None:
    env = _env()
    env["SCHOLIALANG_MCP_SERVER_MODE"] = "off"
    proc = subprocess.Popen(
        [sys.executable, "-m", "scholialang_mcp", "--repo-root", str(FIXTURE)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    try:
        called = _rpc(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "lookup_file_summary",
                    "arguments": {"path": "src/sample.py"},
                },
            },
        )
        text = called["result"]["content"][0]["text"]
        payload = json.loads(text)
        assert payload["status"] == "refused"
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_codex_trace_config_points_at_bundled_trace_server() -> None:
    snippet = codex_trace_config_snippet(ROOT, python_bin="python3")

    assert "[mcp_servers.scholialang]" in snippet
    assert "[mcp_servers.scholialang_atlas]" not in snippet
    assert "plugins/codex/scholialang/scripts/scholialang_mcp_server.py" in snippet
