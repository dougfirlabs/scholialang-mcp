"""Minimum-viable Scholia LSP server.

Scope for the v0.6 LSP MVP:
* hover over location="path:start:end"
* definition for target="..." on Edge and Ref atoms
* grammar validation remains an MCP lint-tool concern

The implementation hand-rolls the small stdio JSON-RPC transport needed for
the MVP. It deliberately avoids editor-specific behavior.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO, Optional
from urllib.parse import unquote, urlparse
from urllib.request import pathname2url

SERVER_VERSION = "0.6.0"

ATTR_RE = re.compile(r"""\b(?P<name>location|target)\s*=\s*(?P<quote>['"])(?P<value>.*?)(?P=quote)""")


@dataclass
class LspState:
    workspace_root: Path
    documents: dict[str, str] = field(default_factory=dict)
    shutdown: bool = False


def path_to_uri(path: Path) -> str:
    return "file://" + pathname2url(str(path.resolve()))


def uri_to_path(uri: str) -> Path:
    parsed = urlparse(uri)
    if parsed.scheme != "file":
        raise ValueError(f"unsupported URI scheme: {parsed.scheme}")
    return Path(unquote(parsed.path))


def _safe_workspace_path(root: Path, value: str) -> Optional[Path]:
    if not value:
        return None
    path_part = value.split("::", 1)[0]
    candidate = Path(path_part)
    if candidate.is_absolute():
        return None
    if any(part == ".." for part in candidate.parts):
        return None
    resolved = (root / candidate).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError:
        return None
    return resolved


def _read_document_text(state: LspState, uri: str) -> str:
    if uri in state.documents:
        return state.documents[uri]
    try:
        return uri_to_path(uri).read_text(encoding="utf-8")
    except OSError:
        return ""


def _line_at(text: str, line_no: int) -> str:
    lines = text.splitlines()
    if line_no < 0 or line_no >= len(lines):
        return ""
    return lines[line_no]


def _find_attr_at(line: str, character: int, name: str) -> Optional[re.Match[str]]:
    fallback: Optional[re.Match[str]] = None
    for match in ATTR_RE.finditer(line):
        if match.group("name") != name:
            continue
        fallback = match
        if match.start("value") <= character <= match.end("value"):
            return match
    return fallback


def _range_for_match(match: re.Match[str]) -> dict[str, Any]:
    return {
        "start": {"line": 0, "character": match.start("value")},
        "end": {"line": 0, "character": match.end("value")},
    }


def _parse_location(value: str) -> Optional[tuple[str, int, int]]:
    try:
        path, start_text, end_text = value.rsplit(":", 2)
        start = int(start_text)
        end = int(end_text)
    except ValueError:
        return None
    if start < 1 or end < start:
        return None
    return path, start, end


def _location_result(root: Path, location_value: str) -> Optional[dict[str, Any]]:
    parsed = _parse_location(location_value)
    if parsed is None:
        return None
    rel_path, start, end = parsed
    target = _safe_workspace_path(root, rel_path)
    if target is None:
        return None
    return {
        "uri": path_to_uri(target),
        "range": {
            "start": {"line": start - 1, "character": 0},
            "end": {"line": end - 1, "character": 0},
        },
    }


def _hover(state: LspState, params: dict[str, Any]) -> Optional[dict[str, Any]]:
    uri = params.get("textDocument", {}).get("uri", "")
    position = params.get("position", {})
    line_no = int(position.get("line", 0))
    character = int(position.get("character", 0))
    text = _read_document_text(state, uri)
    line = _line_at(text, line_no)
    match = _find_attr_at(line, character, "location")
    if match is None:
        return None
    parsed = _parse_location(match.group("value"))
    if parsed is None:
        return None
    rel_path, start, end = parsed
    target = _safe_workspace_path(state.workspace_root, rel_path)
    if target is None or not target.is_file():
        return None
    source_lines = target.read_text(encoding="utf-8").splitlines()
    snippet = "\n".join(source_lines[start - 1 : end])
    return {
        "contents": {
            "kind": "markdown",
            "value": f"`{rel_path}:{start}:{end}`\n\n```text\n{snippet}\n```",
        },
        "range": {
            "start": {"line": line_no, "character": match.start("value")},
            "end": {"line": line_no, "character": match.end("value")},
        },
    }


def _atom_id_location(text: str, atom_id: str) -> Optional[str]:
    pattern = re.compile(
        r"""<(?P<kind>Edge|Ref|Observation|Finding|Hypothesis|Goal|Action|Evidence|Concluding|Review)\b[^>]*\bid\s*=\s*['"]"""
        + re.escape(atom_id)
        + r"""['"][^>]*\blocation\s*=\s*['"](?P<location>[^'"]+)['"]""",
        re.DOTALL,
    )
    match = pattern.search(text)
    if match is None:
        return None
    return match.group("location")


def _definition(state: LspState, params: dict[str, Any]) -> Optional[list[dict[str, Any]]]:
    uri = params.get("textDocument", {}).get("uri", "")
    position = params.get("position", {})
    line_no = int(position.get("line", 0))
    character = int(position.get("character", 0))
    text = _read_document_text(state, uri)
    line = _line_at(text, line_no)
    match = _find_attr_at(line, character, "target")
    if match is None:
        return None
    target_value = match.group("value")

    if "/" in target_value or "." in target_value.split("::", 1)[0]:
        target = _safe_workspace_path(state.workspace_root, target_value)
        if target is not None:
            return [
                {
                    "uri": path_to_uri(target),
                    "range": {
                        "start": {"line": 0, "character": 0},
                        "end": {"line": 0, "character": 0},
                    },
                }
            ]

    location_value = _atom_id_location(text, target_value)
    if location_value is None:
        return None
    location = _location_result(state.workspace_root, location_value)
    return [location] if location is not None else None


def _ok(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _err(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def handle_request(state: LspState, payload: dict[str, Any]) -> Optional[dict[str, Any]]:
    method = str(payload.get("method") or "")
    params = payload.get("params") or {}
    request_id = payload.get("id")

    if method == "initialize":
        root_uri = params.get("rootUri")
        if isinstance(root_uri, str) and root_uri:
            try:
                state.workspace_root = uri_to_path(root_uri).resolve()
            except ValueError:
                pass
        return _ok(
            request_id,
            {
                "capabilities": {
                    "hoverProvider": True,
                    "definitionProvider": True,
                    "textDocumentSync": 1,
                },
                "serverInfo": {"name": "scholialang-lsp", "version": SERVER_VERSION},
            },
        )

    if method == "shutdown":
        state.shutdown = True
        return _ok(request_id, None)
    if method == "exit":
        state.shutdown = True
        return None

    if method == "textDocument/didOpen":
        doc = params.get("textDocument", {})
        uri = doc.get("uri")
        text = doc.get("text")
        if isinstance(uri, str) and isinstance(text, str):
            state.documents[uri] = text
        return None

    if method == "textDocument/hover":
        return _ok(request_id, _hover(state, params))
    if method == "textDocument/definition":
        return _ok(request_id, _definition(state, params))

    if request_id is None:
        return None
    return _err(request_id, -32601, f"unknown method: {method}")


def read_lsp_message(stream: BinaryIO) -> Optional[dict[str, Any]]:
    headers: dict[str, str] = {}
    while True:
        line = stream.readline()
        if not line:
            return None
        if line in (b"\r\n", b"\n"):
            break
        name, _, value = line.decode("ascii").partition(":")
        headers[name.strip().lower()] = value.strip()
    length = int(headers.get("content-length", "0"))
    if length <= 0:
        return None
    body = stream.read(length)
    return json.loads(body.decode("utf-8"))


def write_lsp_message(stream: BinaryIO, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    stream.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body)
    stream.flush()


def serve(state: LspState, stdin: BinaryIO, stdout: BinaryIO) -> int:
    while not state.shutdown:
        payload = read_lsp_message(stdin)
        if payload is None:
            break
        response = handle_request(state, payload)
        if response is not None:
            write_lsp_message(stdout, response)
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scholialang_mcp.lsp")
    parser.add_argument("--workspace-root", default=".")
    args = parser.parse_args(argv)
    state = LspState(workspace_root=Path(args.workspace_root).resolve())
    return serve(state, sys.stdin.buffer, sys.stdout.buffer)


if __name__ == "__main__":
    raise SystemExit(main())
