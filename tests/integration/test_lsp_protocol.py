from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.request import pathname2url


ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests" / "fixtures" / "lsp"
TRACE = FIXTURE / "trace.scholia"


def _uri(path: Path) -> str:
    return "file://" + pathname2url(str(path.resolve()))


def _env() -> dict[str, str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    return env


def _send(proc: subprocess.Popen[bytes], payload: dict[str, object]) -> None:
    assert proc.stdin is not None
    body = json.dumps(payload).encode("utf-8")
    proc.stdin.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body)
    proc.stdin.flush()


def _recv(proc: subprocess.Popen[bytes]) -> dict[str, object]:
    assert proc.stdout is not None
    headers: dict[str, str] = {}
    while True:
        line = proc.stdout.readline()
        assert line
        if line in (b"\r\n", b"\n"):
            break
        name, _, value = line.decode("ascii").partition(":")
        headers[name.lower()] = value.strip()
    body = proc.stdout.read(int(headers["content-length"]))
    return json.loads(body)


def test_lsp_hover_and_definition() -> None:
    text = TRACE.read_text(encoding="utf-8")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "scholialang_mcp.lsp",
            "--workspace-root",
            str(FIXTURE),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_env(),
    )
    try:
        _send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"rootUri": _uri(FIXTURE)},
            },
        )
        init = _recv(proc)
        assert init["result"]["capabilities"]["hoverProvider"] is True
        assert init["result"]["capabilities"]["textDocumentSync"] == {
            "openClose": True,
            "change": 1,
        }

        trace_uri = _uri(TRACE)
        _send(
            proc,
            {
                "jsonrpc": "2.0",
                "method": "textDocument/didOpen",
                "params": {"textDocument": {"uri": trace_uri, "text": text, "version": 1}},
            },
        )

        hover_char = text.splitlines()[0].index("sample.py")
        _send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "textDocument/hover",
                "params": {
                    "textDocument": {"uri": trace_uri},
                    "position": {"line": 0, "character": hover_char},
                },
            },
        )
        hover = _recv(proc)
        assert "return \"sample\"" in hover["result"]["contents"]["value"]

        changed = text.replace("src/sample.py:1:2", "src/sample.py:2:2")
        _send(
            proc,
            {
                "jsonrpc": "2.0",
                "method": "textDocument/didChange",
                "params": {
                    "textDocument": {"uri": trace_uri, "version": 2},
                    "contentChanges": [{"text": changed}],
                },
            },
        )
        _send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "textDocument/hover",
                "params": {
                    "textDocument": {"uri": trace_uri},
                    "position": {"line": 0, "character": hover_char},
                },
            },
        )
        changed_hover = _recv(proc)
        assert "return \"sample\"" in changed_hover["result"]["contents"]["value"]
        assert "def thing" not in changed_hover["result"]["contents"]["value"]

        # A stale full-sync notification must not roll the open buffer back.
        _send(
            proc,
            {
                "jsonrpc": "2.0",
                "method": "textDocument/didChange",
                "params": {
                    "textDocument": {"uri": trace_uri, "version": 1},
                    "contentChanges": [{"text": text}],
                },
            },
        )

        # The buffer must still hold the exact version-2 content and ranges,
        # not merely resolve some URI: the hover response after the stale
        # notification is byte-identical to the version-2 hover response.
        _send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "textDocument/hover",
                "params": {
                    "textDocument": {"uri": trace_uri},
                    "position": {"line": 0, "character": hover_char},
                },
            },
        )
        stale_hover = _recv(proc)
        assert stale_hover["result"] == changed_hover["result"]

        # A layout-shifting edit moves the location attribute rightwards; the
        # reported hover range must track the new columns exactly, and a stale
        # resend of the previous layout must not shift them back.
        shifted = changed.replace(
            '<Observation id="Obs_01" ', '<Observation id="Obs_01" note="shifted" '
        )
        _send(
            proc,
            {
                "jsonrpc": "2.0",
                "method": "textDocument/didChange",
                "params": {
                    "textDocument": {"uri": trace_uri, "version": 3},
                    "contentChanges": [{"text": shifted}],
                },
            },
        )
        shifted_line = shifted.splitlines()[0]
        value_start = shifted_line.index("src/sample.py:2:2")
        expected_range = {
            "start": {"line": 0, "character": value_start},
            "end": {"line": 0, "character": value_start + len("src/sample.py:2:2")},
        }
        shifted_position = {"line": 0, "character": shifted_line.index("sample.py")}
        _send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 8,
                "method": "textDocument/hover",
                "params": {"textDocument": {"uri": trace_uri}, "position": shifted_position},
            },
        )
        shifted_hover = _recv(proc)
        assert shifted_hover["result"]["range"] == expected_range
        assert "src/sample.py:2:2" in shifted_hover["result"]["contents"]["value"]
        _send(
            proc,
            {
                "jsonrpc": "2.0",
                "method": "textDocument/didChange",
                "params": {
                    "textDocument": {"uri": trace_uri, "version": 2},
                    "contentChanges": [{"text": changed}],
                },
            },
        )
        _send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 9,
                "method": "textDocument/hover",
                "params": {"textDocument": {"uri": trace_uri}, "position": shifted_position},
            },
        )
        assert _recv(proc)["result"] == shifted_hover["result"]

        ref_char = text.splitlines()[1].index("Obs_01")
        _send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "textDocument/definition",
                "params": {
                    "textDocument": {"uri": trace_uri},
                    "position": {"line": 1, "character": ref_char},
                },
            },
        )
        definition = _recv(proc)
        assert definition["result"][0]["uri"].endswith("/src/sample.py")
        # Version-2/3 content points Obs_01 at line 2, so the definition range
        # must be the current one — a rollback to version 1 would report 1:2.
        assert definition["result"][0]["range"] == {
            "start": {"line": 1, "character": 0},
            "end": {"line": 1, "character": 0},
        }

        edge_char = text.splitlines()[2].index("sample.py")
        _send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "textDocument/definition",
                "params": {
                    "textDocument": {"uri": trace_uri},
                    "position": {"line": 2, "character": edge_char},
                },
            },
        )
        file_definition = _recv(proc)
        assert file_definition["result"][0]["uri"].endswith("/src/sample.py")

        _send(
            proc,
            {
                "jsonrpc": "2.0",
                "method": "textDocument/didClose",
                "params": {"textDocument": {"uri": trace_uri}},
            },
        )
        _send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "textDocument/hover",
                "params": {
                    "textDocument": {"uri": trace_uri},
                    "position": {"line": 0, "character": hover_char},
                },
            },
        )
        closed_hover = _recv(proc)
        assert "def thing" in closed_hover["result"]["contents"]["value"]
    finally:
        proc.terminate()
        proc.wait(timeout=5)
