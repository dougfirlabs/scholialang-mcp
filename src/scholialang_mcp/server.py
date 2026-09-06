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
# Preferred protocol version = the stable 2026-07-28 release. Older revisions
# stay supported so the adapter is dual-version: pre-handshake-removal hosts
# keep working via ``initialize`` while 2026-07-28 hosts use ``server/discover``
# + per-request ``_meta`` version carriage.
MCP_PROTOCOL_VERSION = "2026-07-28"
SUPPORTED_MCP_PROTOCOL_VERSIONS = (
    "2026-07-28",
    "2025-11-25",
    "2025-06-18",
    "2025-03-26",
    "2024-11-05",
)
SERVER_VERSION = "0.7.3"

# MCP 2026-07-28 ``_meta`` keys (SEP-2575 / SEP-2322 / SEP-2549).
META_PROTOCOL_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CLIENT_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
META_CLIENT_INFO = "io.modelcontextprotocol/clientInfo"
META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"
# JSON-RPC error code for a version the server does not support (renumbered
# -32004 -> -32022 in 2026-07-28's error-code allocation policy).
UNSUPPORTED_PROTOCOL_VERSION = -32022
# CacheableResult hints (SEP-2549). The tool catalog only changes on a server
# upgrade, so a 5-minute freshness window is safe and cuts re-polling. Scope is
# ``private``: this server runs locally against one operator's project, so
# nothing it returns may be shared through a cross-client cache (PRD
# mcp-2026-07-28-prd-01 requires the private scope verified; the catalog is
# harmless today, but private is the uniform safe default for every result
# this server can emit).
TOOLS_LIST_TTL_MS = 300_000
CACHE_SCOPE = "private"

REFUSAL_STATUS = "refused"
REFUSAL_REASON = "disabled_by_mode"
NOT_GENERATED_STATUS = "not_generated_yet"
PROJECT_NOT_SWEPT_STATUS = "atlas_not_yet_swept"


@dataclass
class ScholiaServerConfig:
    """Per-server configuration."""

    repo_root: Path
    allow_regenerate: bool = False
    # Host-only injection. CLI/config snippets do not activate new facets.
    adapters: Any = None


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
                "hint": "regeneration is host-adapter specific in scholialang-mcp v0.7.3",
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


def _server_info() -> dict[str, Any]:
    return {"name": SERVER_NAME, "version": SERVER_VERSION}


def _ok(request_id: Any, result: Any) -> dict[str, Any]:
    # 2026-07-28: every result carries ``resultType`` (SEP-2322) and the
    # server identifies itself in ``_meta`` (SEP-2575). Both are additive —
    # earlier-protocol clients ignore the extra keys and, per spec, treat a
    # missing ``resultType`` as ``"complete"`` — so this stays dual-version.
    if isinstance(result, dict):
        result = dict(result)
        result.setdefault("resultType", "complete")
        meta = dict(result.get("_meta") or {})
        meta.setdefault(META_SERVER_INFO, _server_info())
        result["_meta"] = meta
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _err(
    request_id: Any,
    code: int,
    message: str,
    data: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


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


def _modern_protocol_version(params: dict[str, Any]) -> Optional[str]:
    """Return the per-request modern protocol version, if one is declared."""
    meta = params.get("_meta")
    if isinstance(meta, dict) and meta.get(META_PROTOCOL_VERSION):
        return str(meta.get(META_PROTOCOL_VERSION))
    return None


def _legacy_protocol_version(params: dict[str, Any]) -> Optional[str]:
    requested = params.get("protocolVersion")
    return str(requested) if requested else None


def _unsupported_version(
    request_id: Any,
    requested: str,
    *,
    supported: tuple[str, ...] = SUPPORTED_MCP_PROTOCOL_VERSIONS,
) -> dict[str, Any]:
    return _err(
        request_id,
        UNSUPPORTED_PROTOCOL_VERSION,
        "Unsupported protocol version",
        {"supported": list(supported), "requested": requested},
    )


def _discover_result() -> dict[str, Any]:
    """Final-stable ``server/discover`` payload."""
    return {
        "supportedVersions": list(SUPPORTED_MCP_PROTOCOL_VERSIONS),
        "capabilities": {"tools": {"listChanged": False}},
        "instructions": (
            "Use the Scholia atlas tools to inspect host-generated project summaries."
        ),
        "ttlMs": TOOLS_LIST_TTL_MS,
        "cacheScope": CACHE_SCOPE,
    }


def _handle_request(server: ScholiaMCPServer, payload: dict[str, Any]) -> Optional[dict[str, Any]]:
    method = str(payload.get("method") or "")
    params = payload.get("params") or {}
    request_id = payload.get("id")

    if request_id is None:
        return None
    if not isinstance(params, dict):
        return _err(request_id, -32602, "params must be a JSON object")

    meta = params.get("_meta")
    if meta is not None and not isinstance(meta, dict):
        return _err(request_id, -32602, "_meta must be a JSON object")

    modern_version = _modern_protocol_version(params)
    if modern_version is not None:
        if modern_version != MCP_PROTOCOL_VERSION:
            return _unsupported_version(request_id, modern_version)
        capabilities = (meta or {}).get(META_CLIENT_CAPABILITIES)
        if not isinstance(capabilities, dict):
            return _err(
                request_id,
                -32602,
                f"missing or invalid required _meta field: {META_CLIENT_CAPABILITIES}",
            )
        client_info = (meta or {}).get(META_CLIENT_INFO)
        if client_info is not None and (
            not isinstance(client_info, dict)
            or not isinstance(client_info.get("name"), str)
            or not isinstance(client_info.get("version"), str)
        ):
            return _err(
                request_id,
                -32602,
                f"invalid optional _meta field: {META_CLIENT_INFO}",
            )
        if method in {
            "initialize",
            "ping",
            "logging/setLevel",
            "resources/subscribe",
            "resources/unsubscribe",
        }:
            return _err(request_id, -32601, f"Method not found: {method}")

    # 2026-07-28 MUST: advertise supported versions, capabilities, identity.
    # Also serves as the STDIO backward-compatibility probe for new hosts.
    if method == "server/discover":
        if modern_version != MCP_PROTOCOL_VERSION:
            return _err(
                request_id,
                -32602,
                "server/discover requires 2026-07-28 per-request _meta",
            )
        result = _discover_result()
        if server.config.adapters is not None and resolve_mode() == MODE_ENABLED:
            result["capabilities"].update(server.config.adapters.capabilities())
            result["ttlMs"] = 0  # authority can be revoked between requests
        return _ok(request_id, result)
    # Legacy handshake — retained for pre-2026 hosts (dual-version). New hosts
    # never send it; they call server/discover and carry version in _meta.
    if method == "initialize":
        requested = _legacy_protocol_version(params)
        legacy_versions = tuple(
            version
            for version in SUPPORTED_MCP_PROTOCOL_VERSIONS
            if version != MCP_PROTOCOL_VERSION
        )
        if requested not in legacy_versions:
            return _unsupported_version(
                request_id,
                requested or "",
                supported=legacy_versions,
            )
        return _ok(
            request_id,
            {
                "protocolVersion": requested,
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": _server_info(),
            },
        )
    # ping was removed in 2026-07-28 but kept here as a no-op so pre-2026 hosts
    # that still heartbeat do not break.
    if method == "ping":
        return _ok(request_id, {})
    if method == "tools/list":
        # CacheableResult (SEP-2549): static catalog, private scope (see
        # CACHE_SCOPE). Definition order is the wire order — deterministic
        # across calls and processes per the 2026-07-28 SHOULD.
        tools = list(TOOL_DEFINITIONS)
        adapters = server.config.adapters
        if (modern_version == MCP_PROTOCOL_VERSION and adapters is not None
                and resolve_mode() == MODE_ENABLED and adapters.enabled("tasks")
                and adapters.peer(params, "io.modelcontextprotocol/tasks")):
            tools += list(adapters.tasks.tools.values())
        return _ok(
            request_id,
            {
                "tools": tools,
                "ttlMs": 0 if adapters is not None else TOOLS_LIST_TTL_MS,
                "cacheScope": CACHE_SCOPE,
            },
        )
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

    # Unknown JSON-RPC methods fail with -32601. 0.6.2 additionally dispatched
    # unknown methods as direct tool invocations (audit p7); that undocumented
    # surface is removed — tools are reachable only through ``tools/call``.
    return _err(request_id, -32601, f"unknown method: {method}")


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


def serve_stdio(server: ScholiaMCPServer, source=None, sink=None) -> int:
    """Serve actual JSON-RPC lines, polling committed events while stdin is idle.

    Only the trusted embedding API can inject adapters. A bounded reader queue
    avoids TextIO buffering/select races without creating an HTTP transport.
    """
    import queue
    import threading

    source = sys.stdin if source is None else source
    sink = sys.stdout if sink is None else sink
    adapters = server.config.adapters

    def send(payload):
        sink.write(json.dumps(payload, allow_nan=False) + "\n")
        sink.flush()

    def dispatch(line):
        try:
            payload = json.loads(line, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
            if not isinstance(payload, dict):
                raise ValueError("request must be an object")
        except ValueError:
            send(_err(None, -32700, "invalid_json"))
            return
        responses = None
        if adapters is not None and resolve_mode() == MODE_ENABLED:
            with adapters.lock:
                responses = adapters.handle(payload)
        if responses is None:
            response = _handle_request(server, payload)
            responses = [response] if response is not None else []
        for response in responses:
            send(response)

    if adapters is None:
        for line in source:
            if line.strip():
                dispatch(line)
        return 0

    pending = queue.Queue(maxsize=64)
    def read_lines():
        while True:
            line = source.readline(65_537)
            pending.put(line)
            if not line or len(line) > 65_536:
                return

    threading.Thread(target=read_lines, daemon=True).start()
    try:
        while True:
            try:
                line = pending.get(timeout=0.05)
            except queue.Empty:
                line = None
            if line == "":
                for request_id in list(adapters.events.subscriptions):
                    send(adapters.events.close(request_id))
                return 0
            if line is not None:
                if len(line) > 65_536:
                    send(_err(None, -32600, "request_too_large"))
                    return 1
                if line.strip():
                    dispatch(line)
            with adapters.lock:
                if resolve_mode() != MODE_ENABLED:
                    messages = [adapters.events.close(i) for i in list(adapters.events.subscriptions)]
                else:
                    messages = adapters.events.poll()
            for message in messages:
                send(message)
    except BrokenPipeError:
        return 0


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
    return serve_stdio(server)


if __name__ == "__main__":
    raise SystemExit(main())
