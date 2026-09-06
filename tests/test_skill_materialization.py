"""Cross-host materialization and conformance gates for the public skills.

Covers the PRD gates: two independent materializations of every supported
host output are byte-identical and match the committed generated copies;
manual edits, stale copies, and extra files in a generated tree fail the
check; frontmatter, trigger, reference, tree-shape, and executable-mode
violations are each detected; Codex UI metadata is generated from the final
skill content; and hosts without a native skill surface (Claude Desktop,
Ollama) are reported honestly instead of being claimed as skill installs.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MATERIALIZER = ROOT / "scripts" / "materialize_skills.py"

_SPEC = importlib.util.spec_from_file_location("materialize_skills", MATERIALIZER)
assert _SPEC is not None and _SPEC.loader is not None
mat = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mat)

RELEASE = "0.7.3"
GRAMMAR = "0.7.0"


def _copy_skills(destination: Path) -> Path:
    """A minimal writable checkout carrying only the skill trees."""
    for skills_dir in (mat.CANONICAL_SKILLS_DIR, mat.CODEX_SKILLS_DIR):
        shutil.copytree(
            ROOT / skills_dir,
            destination / skills_dir,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
    return destination


def _tree_bytes(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc"
    }


# ---------------------------------------------------------------------------
# Determinism and freshness of the committed generated copies
# ---------------------------------------------------------------------------


def test_two_independent_materializations_are_byte_identical(tmp_path):
    checkouts = [_copy_skills(tmp_path / run) for run in ("first", "second")]
    for checkout in checkouts:
        report = mat.materialize(checkout)
        assert report["ok"], report["issues"]
    assert _tree_bytes(checkouts[0]) == _tree_bytes(checkouts[1])


def test_repeated_renders_are_byte_identical():
    first_trees, first_provenance = mat.render_all(ROOT)
    second_trees, second_provenance = mat.render_all(ROOT)
    assert first_trees == second_trees
    assert first_provenance == second_provenance


def test_committed_generated_copies_match_a_fresh_render():
    assert mat.check(ROOT) == []


def test_materializing_the_committed_tree_changes_nothing(tmp_path):
    checkout = _copy_skills(tmp_path / "checkout")
    before = _tree_bytes(checkout)
    report = mat.materialize(checkout)
    assert report["ok"], report["issues"]
    assert _tree_bytes(checkout) == before


def test_manual_edit_of_a_generated_copy_is_detected(tmp_path):
    checkout = _copy_skills(tmp_path / "checkout")
    edited = checkout / mat.CODEX_SKILLS_DIR / "scholialang-doctor" / "SKILL.md"
    edited.write_text(edited.read_text(encoding="utf-8") + "\n# drift\n", encoding="utf-8")
    issues = mat.check(checkout)
    assert any("SKILL.md is stale or manually edited" in issue for issue in issues)


def test_stale_generated_copy_after_a_canonical_edit_is_detected(tmp_path):
    checkout = _copy_skills(tmp_path / "checkout")
    canonical = checkout / mat.CANONICAL_SKILLS_DIR / "scholialang-verify" / "SKILL.md"
    canonical.write_text(canonical.read_text(encoding="utf-8") + "\nNew canonical text.\n", encoding="utf-8")
    issues = mat.check(checkout)
    assert any(
        "generated" in issue and "SKILL.md is stale or manually edited" in issue
        for issue in issues
    )


def test_extra_file_in_a_generated_tree_is_detected(tmp_path):
    checkout = _copy_skills(tmp_path / "checkout")
    stray = checkout / mat.CODEX_SKILLS_DIR / "scholialang-doctor" / "notes.txt"
    stray.write_text("stray\n", encoding="utf-8")
    issues = mat.check(checkout)
    assert any("extra file notes.txt" in issue for issue in issues)


def test_manually_edited_generated_openai_yaml_is_detected(tmp_path):
    checkout = _copy_skills(tmp_path / "checkout")
    edited = checkout / mat.CANONICAL_SKILLS_DIR / "scholialang-doctor" / "agents" / "openai.yaml"
    edited.write_text(edited.read_text(encoding="utf-8").replace("Doctor", "Medic"), encoding="utf-8")
    issues = mat.check(checkout)
    assert any(
        "canonical" in issue and "agents/openai.yaml is stale or manually edited" in issue
        for issue in issues
    )


def test_stale_or_missing_provenance_manifest_is_detected(tmp_path):
    checkout = _copy_skills(tmp_path / "checkout")
    provenance = checkout / mat.CODEX_SKILLS_DIR / mat.PROVENANCE_NAME
    provenance.unlink()
    assert any("PROVENANCE.json: missing" in issue for issue in mat.check(checkout))
    provenance.write_text("{}\n", encoding="utf-8")
    assert any(
        "PROVENANCE.json: stale or manually edited" in issue for issue in mat.check(checkout)
    )


# ---------------------------------------------------------------------------
# Provenance and generated Codex UI metadata
# ---------------------------------------------------------------------------


def test_provenance_manifest_records_generator_and_content_hashes():
    manifest = json.loads(
        (ROOT / mat.CODEX_SKILLS_DIR / mat.PROVENANCE_NAME).read_text(encoding="utf-8")
    )
    assert manifest["generated_by"] == "scripts/materialize_skills.py"
    assert manifest["generator_version"] == RELEASE
    assert manifest["grammar_version"] == GRAMMAR
    assert manifest["do_not_edit"] is True
    assert manifest["canonical_source"] == mat.CANONICAL_SKILLS_DIR.as_posix()
    for skill in mat.SKILLS:
        files = manifest["skills"][skill]["files"]
        assert set(files) == {rel.as_posix() for rel in mat._skill_files(ROOT / mat.CODEX_SKILLS_DIR / skill)}
        for rel, digest in files.items():
            payload = (ROOT / mat.CODEX_SKILLS_DIR / skill / rel).read_bytes()
            assert mat._sha256(payload) == digest, rel


def test_openai_yaml_is_generated_from_final_skill_content():
    skill_md = '---\nname: scholialang-doctor\ndescription: x\n---\n# body\n'
    rendered = mat.render_openai_yaml("scholialang-doctor", skill_md).decode("utf-8")
    assert 'display_name: "Scholialang Doctor"' in rendered
    assert 'short_description: "Read-only Scholialang version and capability doctor"' in rendered
    for skill in mat.SKILLS:
        committed = (ROOT / mat.CANONICAL_SKILLS_DIR / skill / "agents" / "openai.yaml").read_bytes()
        assert committed == mat.render_skill_tree(ROOT, skill)["agents/openai.yaml"]


def test_codex_short_description_overlays_cover_exactly_the_public_skills():
    assert set(mat.CODEX_SHORT_DESCRIPTIONS) == set(mat.SKILLS)
    for skill, short in mat.CODEX_SHORT_DESCRIPTIONS.items():
        assert 0 < len(short) <= 80, skill
        assert '"' not in short and "\n" not in short, skill


# ---------------------------------------------------------------------------
# Validation matrix: metadata, triggers, references, tree shape, exec modes
# ---------------------------------------------------------------------------


def test_real_skills_pass_every_validation():
    for skill in mat.SKILLS:
        assert mat.validate_skill(ROOT, skill) == []


def _canonical_skill_md(checkout: Path, skill: str) -> Path:
    return checkout / mat.CANONICAL_SKILLS_DIR / skill / "SKILL.md"


def test_disallowed_frontmatter_key_is_rejected(tmp_path):
    checkout = _copy_skills(tmp_path / "checkout")
    path = _canonical_skill_md(checkout, "scholialang-doctor")
    path.write_text(
        path.read_text(encoding="utf-8").replace("---\n\n#", "unexpected-key: 1\n---\n\n#", 1),
        encoding="utf-8",
    )
    issues = mat.validate_skill(checkout, "scholialang-doctor")
    assert any("disallowed keys ['unexpected-key']" in issue for issue in issues)


def test_trigger_description_must_start_with_use_when(tmp_path):
    checkout = _copy_skills(tmp_path / "checkout")
    path = _canonical_skill_md(checkout, "scholialang-doctor")
    path.write_text(
        path.read_text(encoding="utf-8").replace("description: Use when", "description: Run when", 1),
        encoding="utf-8",
    )
    issues = mat.validate_skill(checkout, "scholialang-doctor")
    assert any("must start with 'Use when'" in issue for issue in issues)


def test_short_trigger_description_is_rejected(tmp_path):
    checkout = _copy_skills(tmp_path / "checkout")
    path = _canonical_skill_md(checkout, "scholialang-doctor")
    text = path.read_text(encoding="utf-8")
    start = text.index("description: ")
    end = text.index("\n", start)
    path.write_text(text[:start] + "description: Use when short." + text[end:], encoding="utf-8")
    issues = mat.validate_skill(checkout, "scholialang-doctor")
    assert any("length" in issue and "outside" in issue for issue in issues)


def test_mismatched_metadata_version_is_rejected(tmp_path):
    checkout = _copy_skills(tmp_path / "checkout")
    path = _canonical_skill_md(checkout, "scholialang-verify")
    path.write_text(
        path.read_text(encoding="utf-8").replace('version: "0.7.3"', 'version: "0.7.1"', 1),
        encoding="utf-8",
    )
    issues = mat.validate_skill(checkout, "scholialang-verify")
    assert any("metadata.version must be '0.7.3'" in issue for issue in issues)


def test_broken_relative_script_reference_is_rejected(tmp_path):
    checkout = _copy_skills(tmp_path / "checkout")
    path = _canonical_skill_md(checkout, "scholialang-doctor")
    path.write_text(
        path.read_text(encoding="utf-8") + "\nRun `python3 scripts/does_not_exist.py`.\n",
        encoding="utf-8",
    )
    issues = mat.validate_skill(checkout, "scholialang-doctor")
    assert any("missing file scripts/does_not_exist.py" in issue for issue in issues)


def test_absolute_path_reference_is_rejected(tmp_path):
    checkout = _copy_skills(tmp_path / "checkout")
    path = _canonical_skill_md(checkout, "scholialang-doctor")
    path.write_text(
        path.read_text(encoding="utf-8") + "\nSee /home/someone/notes.md for details.\n",
        encoding="utf-8",
    )
    issues = mat.validate_skill(checkout, "scholialang-doctor")
    assert any("absolute path" in issue for issue in issues)


def test_forbidden_auxiliary_file_is_rejected(tmp_path):
    checkout = _copy_skills(tmp_path / "checkout")
    aux = checkout / mat.CANONICAL_SKILLS_DIR / "scholialang-doctor" / "README.md"
    aux.write_text("# aux\n", encoding="utf-8")
    issues = mat.validate_skill(checkout, "scholialang-doctor")
    assert any("forbidden auxiliary file README.md" in issue for issue in issues)


def test_executable_bit_on_a_bundled_script_is_rejected(tmp_path):
    checkout = _copy_skills(tmp_path / "checkout")
    script = checkout / mat.CANONICAL_SKILLS_DIR / "scholialang-doctor" / "scripts" / "scholia_doctor.py"
    script.chmod(script.stat().st_mode | 0o111)
    issues = mat.validate_skill(checkout, "scholialang-doctor")
    assert any("carries the executable bit" in issue for issue in issues)


def test_missing_shebang_on_a_bundled_script_is_rejected(tmp_path):
    checkout = _copy_skills(tmp_path / "checkout")
    script = checkout / mat.CANONICAL_SKILLS_DIR / "scholialang-verify" / "scripts" / "scholia_verify.py"
    script.write_text(
        script.read_text(encoding="utf-8").replace("#!/usr/bin/env python3\n", "", 1),
        encoding="utf-8",
    )
    issues = mat.validate_skill(checkout, "scholialang-verify")
    assert any("lacks the python3 shebang" in issue for issue in issues)


def test_write_mode_refuses_to_materialize_an_invalid_canonical_source(tmp_path):
    checkout = _copy_skills(tmp_path / "checkout")
    aux = checkout / mat.CANONICAL_SKILLS_DIR / "scholialang-doctor" / "README.md"
    aux.write_text("# aux\n", encoding="utf-8")
    before = _tree_bytes(checkout / mat.CODEX_SKILLS_DIR)
    report = mat.materialize(checkout)
    assert report["ok"] is False
    assert report["written"] == []
    assert _tree_bytes(checkout / mat.CODEX_SKILLS_DIR) == before


# ---------------------------------------------------------------------------
# Host matrix honesty and the CLI contract
# ---------------------------------------------------------------------------


def test_host_matrix_reports_native_skill_surfaces_honestly():
    by_host = {entry["host"]: entry for entry in mat.HOST_MATRIX}
    assert set(by_host) == {"claude-code", "codex", "claude-desktop", "ollama"}
    assert by_host["claude-code"]["native_skill_surface"] is True
    assert by_host["codex"]["native_skill_surface"] is True
    assert by_host["claude-desktop"]["native_skill_surface"] is False
    assert ".mcpb" in by_host["claude-desktop"]["delivery"]
    assert by_host["ollama"]["native_skill_surface"] is False
    # The disk layout matches the matrix: no skill tree is shipped to a host
    # that cannot load one.
    assert not (ROOT / "plugins" / "claude-desktop" / "scholialang" / "skills").exists()
    assert not (ROOT / "plugins" / "ollama" / "scholialang" / "skills").exists()


def test_desktop_bundle_covers_mcp_capabilities_without_a_skill_surface(tmp_path):
    build_script = ROOT / "scripts" / "build_claude_desktop_mcpb.py"
    spec = importlib.util.spec_from_file_location("build_claude_desktop_mcpb", build_script)
    assert spec is not None and spec.loader is not None
    builder = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(builder)

    staged = tmp_path / "scholialang"
    manifest = builder.stage_bundle(staged)
    assert manifest["version"] == RELEASE
    assert (staged / "src" / "scholialang_mcp_server.py").is_file()
    # Honesty gate: the bundle carries the MCP server, never a skill tree.
    assert not list(staged.rglob("SKILL.md"))


@pytest.mark.timeout(60)
def test_cli_check_exit_codes_and_json_report(tmp_path):
    ok = subprocess.run(
        [sys.executable, str(MATERIALIZER), "--root", str(ROOT), "--check", "--json"],
        capture_output=True,
        text=True,
        timeout=50,
    )
    assert ok.returncode == 0, ok.stderr
    report = json.loads(ok.stdout)
    assert report["ok"] is True and report["issues"] == []
    assert report["mode"] == "check"
    assert [entry["host"] for entry in report["hosts"]] == [
        "claude-code",
        "codex",
        "claude-desktop",
        "ollama",
    ]

    checkout = _copy_skills(tmp_path / "checkout")
    stray = checkout / mat.CODEX_SKILLS_DIR / "scholialang-doctor" / "notes.txt"
    stray.write_text("stray\n", encoding="utf-8")
    stale = subprocess.run(
        [sys.executable, str(MATERIALIZER), "--root", str(checkout), "--check"],
        capture_output=True,
        text=True,
        timeout=50,
    )
    assert stale.returncode == 1
    assert "extra file notes.txt" in stale.stdout
