import json
import tempfile
from pathlib import Path

from scripts.build_claude_desktop_mcpb import ROOT, stage_bundle


def test_desktop_manifest_and_staged_server_are_release_aligned():
    template_manifest = json.loads(
        (
            ROOT
            / "plugins"
            / "claude-desktop"
            / "scholialang"
            / "manifest.json"
        ).read_text(encoding="utf-8")
    )
    assert template_manifest["manifest_version"] == "0.4"
    assert template_manifest["server"]["type"] == "uv"
    assert template_manifest["server"]["entry_point"] == (
        "src/scholialang_mcp_server.py"
    )
    assert template_manifest["server"]["mcp_config"]["command"] == "uv"
    assert template_manifest["tools_generated"] is True

    with tempfile.TemporaryDirectory() as tmp:
        staged = Path(tmp) / "scholialang"
        manifest = stage_bundle(staged)
        assert manifest["version"] == "0.7.1"
        assert (staged / "src" / "scholialang_mcp_server.py").is_file()
        assert (staged / "src" / "_scholia_vendored" / "validator.py").is_file()
        assert not (staged / "src" / "scholialang_mcp_server.py").is_symlink()
