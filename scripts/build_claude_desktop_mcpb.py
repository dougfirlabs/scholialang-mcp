#!/usr/bin/env python3
"""Build the forwardable Scholialang Claude Desktop MCP Bundle."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "plugins" / "claude-desktop" / "scholialang"
CANONICAL_SCRIPTS = ROOT / "plugins" / "claude-code" / "scholialang" / "scripts"
MCPB_PACKAGE = "@anthropic-ai/mcpb@2.1.2"


def _version_from_server(server_path: Path) -> str:
    for line in server_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("SERVER_VERSION = "):
            return line.split("=", 1)[1].strip().strip('"')
    raise RuntimeError("canonical server has no SERVER_VERSION")


def stage_bundle(destination: Path) -> dict[str, object]:
    """Stage one self-contained bundle tree and return its manifest."""
    shutil.copytree(TEMPLATE, destination, dirs_exist_ok=True)
    source_server = CANONICAL_SCRIPTS / "scholialang_mcp_server.py"
    target_src = destination / "src"
    target_src.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source_server, target_src / source_server.name)
    shutil.copytree(
        CANONICAL_SCRIPTS / "_scholia_vendored",
        target_src / "_scholia_vendored",
    )

    manifest_path = destination / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    server_version = _version_from_server(source_server)
    if manifest["version"] != server_version:
        raise RuntimeError(
            f"manifest/server version drift: {manifest['version']} != {server_version}"
        )
    return manifest


def build(output: Path) -> str:
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="scholialang-mcpb-") as tmp:
        staged = Path(tmp) / "scholialang"
        stage_bundle(staged)
        subprocess.run(
            ["npx", "--yes", MCPB_PACKAGE, "validate", str(staged)],
            cwd=ROOT,
            check=True,
        )
        subprocess.run(
            ["npx", "--yes", MCPB_PACKAGE, "pack", str(staged), str(output)],
            cwd=ROOT,
            check=True,
        )
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    return digest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "dist" / "scholialang-claude-desktop-0.7.2.mcpb",
    )
    args = parser.parse_args()
    digest = build(args.output)
    print(f"artifact={args.output.resolve()}")
    print(f"sha256={digest}")


if __name__ == "__main__":
    main()
