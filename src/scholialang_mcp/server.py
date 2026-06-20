"""Scholia atlas MCP server.

The server is intentionally host-neutral: it reads generic atlas artifacts
when they exist and otherwise returns structured "not generated yet" payloads.
Generation is delegated to host adapters because model auth, budget controls,
and repository indexing differ across clients.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional
from urllib.parse import quote

ENV_MODE = "SCHOLIALANG_MCP_SERVER_MODE"
MODE_OFF = "off"
MODE_ENABLED = "enabled"
VALID_MODES = (MODE_OFF, MODE_ENABLED)

SERVER_NAME = "mcp__scholialang__atlas"
MCP_PROTOCOL_VERSION = "2024-11-05"
SUPPORTED_MCP_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
SERVER_VERSION = "0.6.1"

REFUSAL_STATUS = "refused"
REFUSAL_REASON = "disabled_by_mode"
NOT_GENERATED_STATUS = "not_generated_yet"
PROJECT_NOT_SWEPT_STATUS = "atlas_not_yet_swept"


@dataclass
class ScholiaServerConfig:
    """Per-server configuration."""

    repo_root: Path
    allow_regenerate: bool = False


def resolve_mode() -> str:
    raw = os.environ.get(ENV_MODE, MODE_ENABLED)
    candidate = (raw or "").strip().lower()
    if candidate in VALID_MODES:
        return candidate
    return MODE_ENABLED


def _disabled_response() -> dict[str, Any]:
    return {
        "status": REFUSAL_STATUS,
        "reason": REFUSAL_REASON,
        "server": SERVER_NAME,
        "error": "scholialang_mcp_disabled",
    }


def _project_not_swept_response(repo_root: Path) -> dict[str, Any]:
    return {
        "status": PROJECT_NOT_SWEPT_STATUS,
        "project_root": str(repo_root),
        "hint": "generate a .scholia-atlas/tree.json artifact for this workspace first",
    }


def _safe_repo_path(repo_root: Path, value: str) -> Optional[Path]:
    if not value:
        return None
    candidate = Path(value)
    if candidate.is_absolute():
        return None
    if any(part == ".." for part in candidate.parts):
        return None
    resolved = (repo_root / candidate).resolve()
    try:
        resolved.relative_to(repo_root.resolve())
    except ValueError:
        return None
    return resolved


def _encoded_artifact_path(repo_root: Path, corpus: str, key: str) -> Path:
    encoded = quote(key.strip("/"), safe="")
    return repo_root / ".scholia-atlas" / corpus / f"{encoded}.json"


def _read_json_or_text(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        data = json.loads(text)
        if isinstance(data, dict):
            return data
        return {"value": data}
    return {"text": text}


def _artifact_response(source_path: Path, artifact_path: Path) -> dict[str, Any]:
    payload = _read_json_or_text(artifact_path)
    response: dict[str, Any] = {
        "atlas_path": str(artifact_path),
        "source_path": str(source_path),
        "artifact": payload,
    }
    for key in (
        "granularity",
        "prose_preamble",
        "scholia_codeblock",
        "metadata",
        "skipped",
        "skip_reason",
    ):
        if key in payload:
            response[key] = payload[key]
    return response


@dataclass
class ScholiaMCPServer:
    """MCP tool implementation for Scholia atlas lookups."""

    config: ScholiaServerConfig

    def _project_swept(self) -> bool:
        repo = self.config.repo_root
        return (repo / ".scholia-atlas" / "tree.json").is_file() or (
            repo / ".ot-codex.tree"
        ).is_file()

    def _guard(self) -> Optional[dict[str, Any]]:
        if resolve_mode() != MODE_ENABLED:
            return _disabled_response()
        if not self._project_swept():
            return _project_not_swept_response(self.config.repo_root)
        return None

    def lookup_file_summary(self, path: str) -> dict[str, Any]:
        guard = self._guard()
        if guard is not None:
            return guard
        source = _safe_repo_path(self.config.repo_root, path)
        if source is None or not source.is_file():
            return {"error": "source_not_found", "path": path}
        artifact = _encoded_artifact_path(self.config.repo_root, "files", path)
        if not artifact.is_file():
            return {
                "status": NOT_GENERATED_STATUS,
                "error": "atlas_artifact_not_found",
                "path": path,
                "atlas_path": str(artifact),
            }
        return _artifact_response(source, artifact)

    def lookup_directory_summary(self, path: str) -> dict[str, Any]:
        guard = self._guard()
        if guard is not None:
            return guard
        source = _safe_repo_path(self.config.repo_root, path)
        if source is None or not source.is_dir():
            return {"error": "directory_not_found", "path": path}
        artifact = _encoded_artifact_path(self.config.repo_root, "directories", path)
        if not artifact.is_file():
            return {
                "status": NOT_GENERATED_STATUS,
                "error": "directory_atlas_not_found",
                "path": path,
                "atlas_path": str(artifact),
            }
        return _artifact_response(source, artifact)

    def lookup_feature_summary(self, feature: str) -> dict[str, Any]:
        guard = self._guard()
        if guard is not None:
            return guard
        safe_feature = feature.strip().replace("/", "_")
        if not safe_feature:
            return {"error": "feature_required"}
        artifact = self.config.repo_root / ".scholia-atlas" / "features" / f"{safe_feature}.json"
        if not artifact.is_file():
            return {
                "status": NOT_GENERATED_STATUS,
                "error": "feature_atlas_not_found",
                "feature": feature,
            }
        return _artifact_response(self.config.repo_root, artifact)

    def lookup_kb_summary(self, path: str) -> dict[str, Any]:
        return self._lookup_non_code_summary(path, corpus="kb", default_dir=Path("kb") / "posts")

    def lookup_prd_summary(self, path: str) -> dict[str, Any]:
        return self._lookup_non_code_summary(path, corpus="prds", default_dir=Path("prds"))

    def lookup_doc_summary(self, path: str) -> dict[str, Any]:
        return self._lookup_non_code_summary(path, corpus="docs", default_dir=Path("docs"))

    def _lookup_non_code_summary(
        self,
        path: str,
        *,
        corpus: str,
        default_dir: Path,
    ) -> dict[str, Any]:
        guard = self._guard()
        if guard is not None:
            return guard
        source = _safe_repo_path(self.config.repo_root, path)
        if source is None or not source.is_file():
            basename_source = _safe_repo_path(self.config.repo_root, str(default_dir / path))
            source = basename_source if basename_source and basename_source.is_file() else None
        if source is None:
            return {"error": "source_not_found", "path": path, "corpus": corpus}
        rel = source.relative_to(self.config.repo_root).as_posix()
        artifact = _encoded_artifact_path(self.config.repo_root, corpus, rel)
        if not artifact.is_file():
            return {
                "status": NOT_GENERATED_STATUS,
                "error": f"{corpus}_atlas_not_found",
                "path": rel,
                "atlas_path": str(artifact),
            }
        response = _artifact_response(source, artifact)
        response["corpus"] = corpus
        return response

    def get_tree(self) -> dict[str, Any]:
        if resolve_mode() != MODE_ENABLED:
            return _disabled_response()
        repo = self.config.repo_root
        tree_path = repo / ".scholia-atlas" / "tree.json"
        if not tree_path.is_file():
            legacy = repo / ".ot-codex.tree"
            tree_path = legacy if legacy.is_file() else tree_path
        if not tree_path.is_file():
            return _project_not_swept_response(repo)
        try:
            payload = json.loads(tree_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            return {"error": "tree_malformed", "detail": str(exc)}
        return {"tree": payload, "atlas_path": str(tree_path)}

    def regenerate(self, path: str) -> dict[str, Any]:
        if resolve_mode() != MODE_ENABLED:
            return _disabled_response()
        if not self.config.allow_regenerate:
            return {
                "status": "unsupported",
                "error": "regenerate_unavailable",
                "path": path,
                "hint": "regeneration is host-adapter specific in scholialang-mcp v0.6.1",
            }
        return {"status": "accepted", "path": path}


TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "lookup_file_summary",
        "description": "Return the Scholia atlas summary for a workspace-relative file.",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "lookup_directory_summary",
        "description": "Return the Scholia atlas summary for a workspace-relative directory.",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "lookup_feature_summary",
        "description": "Return the Scholia atlas summary for a named cross-file feature.",
        "inputSchema": {
            "type": "object",
            "properties": {"feature": {"type": "string"}},
            "required": ["feature"],
        },
    },
    {
        "name": "lookup_kb_summary",
        "description": "Return the Scholia atlas summary for a knowledge-base document.",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "lookup_prd_summary",
        "description": "Return the Scholia atlas summary for a planning document.",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "lookup_doc_summary",
        "description": "Return the Scholia atlas summary for a documentation file.",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "get_tree",
        "description": "Return the workspace-level Scholia atlas tree.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "regenerate",
        "description": "Ask a host adapter to regenerate a Scholia atlas artifact for one path.",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
]


def _ok(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _err(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def _invoke_tool(server: ScholiaMCPServer, name: str, args: dict[str, Any]) -> dict[str, Any]:
    if name == "lookup_file_summary":
        return server.lookup_file_summary(str(args.get("path", "")))
    if name == "lookup_directory_summary":
        return server.lookup_directory_summary(str(args.get("path", "")))
    if name == "lookup_feature_summary":
        return server.lookup_feature_summary(str(args.get("feature", "")))
    if name == "lookup_kb_summary":
        return server.lookup_kb_summary(str(args.get("path", "")))
    if name == "lookup_prd_summary":
        return server.lookup_prd_summary(str(args.get("path", "")))
    if name == "lookup_doc_summary":
        return server.lookup_doc_summary(str(args.get("path", "")))
    if name == "get_tree":
        return server.get_tree()
    if name == "regenerate":
        return server.regenerate(str(args.get("path", "")))
    raise KeyError(name)


def _negotiated_protocol_version(params: dict[str, Any]) -> str:
    requested = params.get("protocolVersion")
    if requested in SUPPORTED_MCP_PROTOCOL_VERSIONS:
        return str(requested)
    return MCP_PROTOCOL_VERSION


def _handle_request(server: ScholiaMCPServer, payload: dict[str, Any]) -> Optional[dict[str, Any]]:
    method = str(payload.get("method") or "")
    params = payload.get("params") or {}
    request_id = payload.get("id")

    if request_id is None:
        return None
    if not isinstance(params, dict):
        return _err(request_id, -32602, "params must be a JSON object")

    if method == "initialize":
        return _ok(
            request_id,
            {
                "protocolVersion": _negotiated_protocol_version(params),
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            },
        )
    if method == "ping":
        return _ok(request_id, {})
    if method == "tools/list":
        return _ok(request_id, {"tools": list(TOOL_DEFINITIONS)})
    if method == "tools/call":
        tool_name = str(params.get("name", ""))
        tool_args = params.get("arguments") or {}
        if not isinstance(tool_args, dict):
            return _err(request_id, -32602, "tools/call arguments must be a JSON object")
        try:
            result = _invoke_tool(server, tool_name, tool_args)
        except KeyError:
            return _err(request_id, -32602, f"unknown tool: {tool_name}")
        return _ok(
            request_id,
            {
                "content": [{"type": "text", "text": json.dumps(result, indent=2)}],
                "isError": False,
            },
        )

    try:
        result = _invoke_tool(server, method, params)
    except KeyError:
        return _err(request_id, -32601, f"unknown method: {method}")
    return _ok(request_id, result)


def codex_config_snippet(repo_root: Path, *, python_bin: Optional[str] = None) -> str:
    bin_path = python_bin or sys.executable or "python"
    root = str(repo_root.resolve())
    return (
        "[mcp_servers.scholialang_atlas]\n"
        f'command = "{bin_path}"\n'
        f'args = ["-m", "scholialang_mcp", "--repo-root", "{root}"]\n'
        "[mcp_servers.scholialang_atlas.env]\n"
        f'{ENV_MODE} = "enabled"\n'
    )


def codex_trace_config_snippet(repo_root: Path, *, python_bin: Optional[str] = None) -> str:
    bin_path = python_bin or sys.executable or "python3"
    script = (
        repo_root.resolve()
        / "plugins"
        / "codex"
        / "scholialang"
        / "scripts"
        / "scholialang_mcp_server.py"
    )
    return (
        "[mcp_servers.scholialang]\n"
        f"command = {json.dumps(bin_path)}\n"
        f"args = [{json.dumps(str(script))}]\n"
    )


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scholialang_mcp")
    parser.add_argument(
        "subcommand",
        nargs="?",
        default="serve",
        choices=("serve", "check", "codex-config", "codex-trace-config"),
    )
    parser.add_argument("--repo-root", default=".", help="Workspace root to serve.")
    args = parser.parse_args(argv)

    repo = Path(args.repo_root).resolve()
    if not repo.is_dir():
        print(json.dumps({"error": "repo_root_not_a_directory", "path": str(repo)}))
        return 1

    if args.subcommand == "check":
        print(
            json.dumps(
                {
                    "mode": resolve_mode(),
                    "repo_root": str(repo),
                    "project_swept": ScholiaMCPServer(ScholiaServerConfig(repo))._project_swept(),
                },
                indent=2,
            )
        )
        return 0

    if args.subcommand == "codex-config":
        print(codex_config_snippet(repo))
        return 0

    if args.subcommand == "codex-trace-config":
        print(codex_trace_config_snippet(repo))
        return 0

    server = ScholiaMCPServer(ScholiaServerConfig(repo))
    if resolve_mode() != MODE_ENABLED:
        sys.stderr.write(f"warning: {ENV_MODE} is not enabled; every request will be refused\n")
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            sys.stdout.write(json.dumps({"error": f"bad json: {exc}"}) + "\n")
            sys.stdout.flush()
            continue
        if not isinstance(payload, dict):
            sys.stdout.write(json.dumps({"error": "request must be an object"}) + "\n")
            sys.stdout.flush()
            continue
        response = _handle_request(server, payload)
        if response is None:
            continue
        sys.stdout.write(json.dumps(response) + "\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
