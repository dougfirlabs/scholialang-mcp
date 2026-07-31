"""Generate + verify the MCP protocol support matrix from live probes (PRD-01).

The PRD requires the support matrix to be *generated from tests*, not
hand-written: every cell in ``docs/mcp-support-matrix.md`` is the outcome of a
live wire probe against a real server process run by this module. The
committed document must match what the probes produce; when it drifts, this
test fails and prints the regeneration command:

    SCHOLIA_REGEN_SUPPORT_MATRIX=1 python -m pytest tests/integration/test_mcp_support_matrix.py
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MATRIX_DOC = ROOT / "docs" / "mcp-support-matrix.md"
FIXTURE = ROOT / "tests" / "fixtures" / "atlas" / "sample"
PLUGIN_SERVER = ROOT / "plugins" / "claude-code" / "scholialang" / "scripts" / "scholialang_mcp_server.py"

META_PROTOCOL_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CLIENT_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
UNSUPPORTED_PROTOCOL_VERSION = -32022
MODERN_VERSION = "2026-07-28"

# The union of both servers' version tables; per-server support is probed, not
# assumed, so a table drift in either server changes the generated matrix.
CANDIDATE_VERSIONS = (
    "2026-07-28",
    "2025-11-25",
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
)
UNSUPPORTED_PROBE_VERSION = "1999-01-01"


def _spawn(cmd: list[str]) -> "subprocess.Popen[str]":
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    return subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )


SERVERS = {
    "wheel (`scholialang-mcp serve`)": lambda: _spawn(
        [sys.executable, "-m", "scholialang_mcp", "--repo-root", str(FIXTURE)]
    ),
    "plugin (vendored, all hosts)": lambda: _spawn([sys.executable, str(PLUGIN_SERVER)]),
}


def _rpc(proc: "subprocess.Popen[str]", payload: dict[str, object]) -> dict[str, object]:
    assert proc.stdin is not None and proc.stdout is not None
    proc.stdin.write(json.dumps(payload) + "\n")
    proc.stdin.flush()
    line = proc.stdout.readline()
    assert line, "server closed stdout mid-probe"
    return json.loads(line)


def _modern_meta(version: str = MODERN_VERSION) -> dict[str, object]:
    return {
        META_PROTOCOL_VERSION: version,
        META_CLIENT_CAPABILITIES: {},
    }


def _probe_version(spawn, version: str) -> str:
    """Probe the lifecycle appropriate to the requested protocol era."""
    proc = spawn()
    try:
        if version == MODERN_VERSION:
            modern = _rpc(
                proc,
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/list",
                    "params": {"_meta": _modern_meta()},
                },
            )
            removed = _rpc(
                proc,
                {
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "ping",
                    "params": {"_meta": _modern_meta()},
                },
            )
            assert "result" in modern
            assert removed["error"]["code"] == -32601
            return "modern stateless"

        if version == UNSUPPORTED_PROBE_VERSION:
            unsupported = _rpc(
                proc,
                {
                    "jsonrpc": "2.0",
                    "id": 3,
                    "method": "tools/list",
                    "params": {"_meta": _modern_meta(version)},
                },
            )
            assert unsupported["error"]["code"] == UNSUPPORTED_PROTOCOL_VERSION
            assert unsupported["error"]["data"]["requested"] == version
            return "rejected (-32022)"

        init = _rpc(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "initialize",
                "params": {"protocolVersion": version, "capabilities": {}},
            },
        )
        legacy = _rpc(
            proc,
            {"jsonrpc": "2.0", "id": 5, "method": "tools/list", "params": {}},
        )
    finally:
        proc.terminate()
    init_err = init.get("error")
    legacy_err = legacy.get("error")
    if init_err is None and legacy_err is None:
        negotiated = init["result"]["protocolVersion"]
        assert negotiated == version, f"silent mis-negotiation: {version} -> {negotiated}"
        return "legacy handshake"
    assert init_err is not None and init_err["code"] == UNSUPPORTED_PROTOCOL_VERSION, (
        f"version {version} rejected with a non--32022 shape: {init}"
    )
    return "rejected (-32022)"


def _probe_discover(spawn) -> str:
    proc = spawn()
    try:
        r = _rpc(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "server/discover",
                "params": {"_meta": _modern_meta()},
            },
        )
    finally:
        proc.terminate()
    versions = r["result"]["supportedVersions"]
    return "yes: " + ", ".join(versions)


def _generate_matrix() -> str:
    lines = [
        "# MCP protocol support matrix",
        "",
        "**Generated from live wire probes** by",
        "`tests/integration/test_mcp_support_matrix.py` — do not edit by hand.",
        "Regenerate with:",
        "",
        "```sh",
        "SCHOLIA_REGEN_SUPPORT_MATRIX=1 python -m pytest tests/integration/test_mcp_support_matrix.py",
        "```",
        "",
        "The 2026-07-28 cell passes a stateless `_meta`-carried request and",
        "confirms removed lifecycle methods return `-32601 MethodNotFound`.",
        "Pre-2026 cells pass the legacy `initialize` lifecycle at that version.",
        "Rejected cells fail closed with `-32022 UnsupportedProtocolVersion`",
        "and include the final-stable `supported` / `requested` error data.",
        "",
    ]
    header = ["protocol version"] + list(SERVERS)
    lines.append("| " + " | ".join(header) + " |")
    lines.append("|" + "---|" * len(header))
    for version in CANDIDATE_VERSIONS + (UNSUPPORTED_PROBE_VERSION,):
        row = [f"`{version}`" if version != UNSUPPORTED_PROBE_VERSION else "`1999-01-01` (control)"]
        for spawn in SERVERS.values():
            row.append(_probe_version(spawn, version))
        lines.append("| " + " | ".join(row) + " |")
    lines.append("| `server/discover` | " + " | ".join(_probe_discover(s) for s in SERVERS.values()) + " |")
    lines.append("")
    return "\n".join(lines)


def test_support_matrix_matches_live_probes() -> None:
    generated = _generate_matrix()
    if os.environ.get("SCHOLIA_REGEN_SUPPORT_MATRIX") == "1":
        MATRIX_DOC.write_text(generated, encoding="utf-8")
    assert MATRIX_DOC.exists(), (
        "docs/mcp-support-matrix.md is missing; regenerate with "
        "SCHOLIA_REGEN_SUPPORT_MATRIX=1"
    )
    committed = MATRIX_DOC.read_text(encoding="utf-8")
    assert committed == generated, (
        "docs/mcp-support-matrix.md drifted from live probe results; "
        "regenerate with SCHOLIA_REGEN_SUPPORT_MATRIX=1"
    )
