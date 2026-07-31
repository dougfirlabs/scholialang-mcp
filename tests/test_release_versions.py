"""Release-version parity checks for all non-vendored public surfaces."""

from __future__ import annotations

import ast
import json
import tomllib
from pathlib import Path

import scholialang_mcp


PACKAGE_VERSION = "0.7.0"
SCHOLIA_VERSION = "0.6.2"
ROOT = Path(__file__).resolve().parents[1]


def _constant(path: Path, name: str) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not found in {path.relative_to(ROOT)}")


def _json(path: str) -> dict:
    return json.loads((ROOT / path).read_text(encoding="utf-8"))


def test_python_package_versions_and_dependency_are_aligned():
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert metadata["version"] == PACKAGE_VERSION
    assert metadata["dependencies"] == [f"scholialang>={SCHOLIA_VERSION},<0.7"]
    assert scholialang_mcp.__version__ == PACKAGE_VERSION
    assert _constant(ROOT / "src/scholialang_mcp/server.py", "SERVER_VERSION") == PACKAGE_VERSION
    assert _constant(ROOT / "src/scholialang_mcp/lsp/server.py", "SERVER_VERSION") == PACKAGE_VERSION


def test_plugin_and_marketplace_versions_are_aligned():
    assert _json(".claude-plugin/marketplace.json")["metadata"]["version"] == PACKAGE_VERSION
    assert (
        _json("plugins/claude-code/scholialang/.claude-plugin/plugin.json")["version"]
        == PACKAGE_VERSION
    )
    assert (
        _json("plugins/codex/scholialang/.codex-plugin/plugin.json")["version"]
        == PACKAGE_VERSION
    )

    plugin_servers = [
        ROOT / "plugins" / host / "scholialang" / "scripts" / "scholialang_mcp_server.py"
        for host in ("claude-code", "codex", "ollama")
    ]
    assert {
        _constant(server, "SERVER_VERSION")
        for server in plugin_servers
    } == {PACKAGE_VERSION}
