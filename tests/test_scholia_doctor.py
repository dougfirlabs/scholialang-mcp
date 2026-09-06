"""Contract tests for the read-only scholialang-doctor skill script.

Covers the PRD gates: healthy / mismatched / stale / absent-surface / opt-out
fixtures map to specific pass, not_ready, or fail reasons; the probe is
read-only and never imports code from an untrusted current working directory;
output never carries environment secret values; and installed-artifact
inspection works from explicit distribution metadata paths.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
CANONICAL_SKILL = ROOT / "plugins" / "claude-code" / "scholialang" / "skills" / "scholialang-doctor"
CODEX_SKILL = ROOT / "plugins" / "codex" / "scholialang" / "skills" / "scholialang-doctor"
DOCTOR_SCRIPT = CANONICAL_SKILL / "scripts" / "scholia_doctor.py"

_SPEC = importlib.util.spec_from_file_location("scholia_doctor", DOCTOR_SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
doctor = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(doctor)

RELEASE = "0.7.3"
GRAMMAR = "0.6.2"

_PYPROJECT_TEMPLATE = """\
[project]
name = "scholialang-mcp"
version = "{version}"
dependencies = ["scholialang>={version},<0.8"]

[project.scripts]
{scripts}
"""

_SKILL_MD_TEMPLATE = """\
---
name: scholialang-doctor
description: fixture doctor skill
metadata:
  version: "{version}"
  grammar: "0.6.2"
---

# fixture
"""


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def make_checkout(
    root: Path,
    *,
    version: str = RELEASE,
    init_version: str | None = None,
    plugin_versions: dict[str, str] | None = None,
    skill_version: str | None = None,
    codex_skill: str = "identical",
    scripts: tuple[str, ...] = ("scholialang-mcp", "scholialang-lsp"),
) -> Path:
    script_lines = "\n".join(
        f'{name} = "scholialang_mcp.{name.rsplit("-", 1)[1]}:main"' for name in scripts
    )
    _write(root / "pyproject.toml", _PYPROJECT_TEMPLATE.format(version=version, scripts=script_lines))
    _write(
        root / "src/scholialang_mcp/__init__.py",
        f'__version__ = "{init_version or version}"\n',
    )
    _write(root / "src/scholialang_mcp/server.py", "def main():\n    pass\n")
    _write(root / "src/scholialang_mcp/lsp/server.py", "def main():\n    pass\n")

    plugin_versions = plugin_versions or {}
    _write(
        root / "plugins/claude-code/scholialang/.claude-plugin/plugin.json",
        json.dumps({"version": plugin_versions.get("claude-code", version)}),
    )
    _write(
        root / "plugins/codex/scholialang/.codex-plugin/plugin.json",
        json.dumps({"version": plugin_versions.get("codex", version)}),
    )
    _write(
        root / "plugins/claude-desktop/scholialang/manifest.json",
        json.dumps({"version": plugin_versions.get("claude-desktop", version)}),
    )
    for host in ("claude-code", "codex", "ollama"):
        _write(
            root / "plugins" / host / "scholialang/scripts/_scholia_vendored/UPSTREAM.json",
            json.dumps({"validator_version": version}),
        )
    _write(
        root / "plugins/claude-code/scholialang/scripts/scholialang_mcp_server.py",
        "SERVER_VERSION = " + repr(version) + "\n",
    )
    _write(
        root / f"tests/fixtures/scholialang-spec/v{GRAMMAR}/UPSTREAM.json",
        json.dumps({"spec_version": GRAMMAR}),
    )

    skill_md = _SKILL_MD_TEMPLATE.format(version=skill_version or version)
    canonical = root / "plugins/claude-code/scholialang/skills/scholialang-doctor"
    _write(canonical / "SKILL.md", skill_md)
    _write(canonical / "scripts/scholia_doctor.py", "# fixture stub\n")
    _write(canonical / "agents/openai.yaml", "interface:\n")
    codex = root / "plugins/codex/scholialang/skills/scholialang-doctor"
    if codex_skill == "identical":
        _write(codex / "SKILL.md", skill_md)
        _write(codex / "scripts/scholia_doctor.py", "# fixture stub\n")
        _write(codex / "agents/openai.yaml", "interface:\n")
    elif codex_skill == "stale":
        _write(codex / "SKILL.md", skill_md + "\n# drifted\n")
        _write(codex / "scripts/scholia_doctor.py", "# fixture stub\n")
        _write(codex / "agents/openai.yaml", "interface:\n")
    elif codex_skill == "missing":
        pass
    else:  # pragma: no cover - guard against typos in test parameters
        raise AssertionError(codex_skill)
    return root


def _report(root: Path, tmp_path: Path, **kwargs) -> dict:
    kwargs.setdefault("mode", "repo")
    kwargs.setdefault("root", root)
    kwargs.setdefault("data_dir", tmp_path / "empty-data-dir")
    return doctor.build_report(**kwargs)


def _reasons(report: dict) -> set[tuple[str, str]]:
    return {(r["facet"], r["severity"]) for r in report["overall"]["reasons"]}


def _messages(report: dict) -> str:
    return " | ".join(r["message"] for r in report["overall"]["reasons"])


@pytest.fixture(autouse=True)
def _no_ambient_optout(monkeypatch):
    monkeypatch.delenv("SCHOLIA_AUTOEMIT", raising=False)


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(root)): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


# ---------------------------------------------------------------------------
# Repository-checkout fixtures
# ---------------------------------------------------------------------------


def test_healthy_fixture_passes_with_distinct_version_axes(tmp_path):
    root = make_checkout(tmp_path / "repo")
    report = _report(root, tmp_path)

    assert report["overall"] == {"status": "pass", "reasons": []}
    facets = report["facets"]
    assert facets["grammar"]["version"] == GRAMMAR
    assert facets["mcp_package"]["version"] == RELEASE
    assert facets["plugin"]["version"] == RELEASE
    assert facets["vendored_validator"]["version"] == RELEASE
    assert facets["skill"]["version"] == RELEASE
    assert facets["grammar"]["version"] != facets["mcp_package"]["version"]
    for name in (
        "grammar",
        "python_package",
        "mcp_package",
        "plugin",
        "vendored_validator",
        "skill",
        "mcp_entry_point",
        "lsp_entry_point",
        "auto_emit",
        "database",
        "compatibility",
    ):
        assert {"supported", "present", "version", "compatible", "detail"} <= set(facets[name])


def test_grammar_release_difference_is_never_labeled_a_downgrade(tmp_path):
    report = _report(make_checkout(tmp_path / "repo"), tmp_path)
    text = json.dumps(report)
    assert text.count("downgrade") == text.count("not a downgrade")
    assert report["facets"]["compatibility"]["grammar_version"] == GRAMMAR
    assert report["facets"]["compatibility"]["release_version"] == RELEASE


def test_mismatched_plugin_version_fails_with_both_versions_named(tmp_path):
    root = make_checkout(tmp_path / "repo", plugin_versions={"codex": "0.7.1"})
    report = _report(root, tmp_path)

    assert report["overall"]["status"] == "fail"
    assert ("plugin", "fail") in _reasons(report)
    assert "0.7.1" in _messages(report) and RELEASE in _messages(report)


def test_stale_skill_version_is_not_ready(tmp_path):
    root = make_checkout(tmp_path / "repo", skill_version="0.7.1")
    report = _report(root, tmp_path)

    assert report["overall"]["status"] == "not_ready"
    assert ("skill", "not_ready") in _reasons(report)
    assert "stale" in _messages(report)


def test_stale_codex_skill_copy_is_not_ready(tmp_path):
    root = make_checkout(tmp_path / "repo", codex_skill="stale")
    report = _report(root, tmp_path)

    assert report["overall"]["status"] == "not_ready"
    assert ("skill", "not_ready") in _reasons(report)
    assert report["facets"]["skill"]["host_copies_identical"] is False


def test_missing_mcp_and_lsp_entry_points_are_specific_not_ready(tmp_path):
    root = make_checkout(tmp_path / "repo", scripts=())
    report = _report(root, tmp_path)

    assert report["overall"]["status"] == "not_ready"
    assert ("mcp_entry_point", "not_ready") in _reasons(report)
    assert ("lsp_entry_point", "not_ready") in _reasons(report)


def test_opt_out_file_is_specific_not_ready_without_mutation(tmp_path):
    root = make_checkout(tmp_path / "repo")
    (root / ".scholia-off").write_text("", encoding="utf-8")
    before = _snapshot(root)
    report = _report(root, tmp_path)

    assert report["overall"]["status"] == "not_ready"
    assert ("auto_emit", "not_ready") in _reasons(report)
    assert ".scholia-off" in _messages(report)
    assert report["facets"]["auto_emit"]["state"] == "disabled"
    assert _snapshot(root) == before


def test_opt_out_environment_is_specific_not_ready(tmp_path, monkeypatch):
    monkeypatch.setenv("SCHOLIA_AUTOEMIT", "0")
    report = _report(make_checkout(tmp_path / "repo"), tmp_path)

    assert report["overall"]["status"] == "not_ready"
    assert ("auto_emit", "not_ready") in _reasons(report)
    assert "SCHOLIA_AUTOEMIT" in _messages(report)


def test_doctor_is_read_only_and_reports_absent_database(tmp_path):
    root = make_checkout(tmp_path / "repo")
    data_dir = tmp_path / "data-dir-that-does-not-exist"
    before = _snapshot(root)
    report = _report(root, tmp_path, data_dir=data_dir)

    database = report["facets"]["database"]
    assert database["supported"] is True
    assert database["present"] is False
    assert database["size_bytes"] is None
    # An absent optional surface is metadata, never a generic failure.
    assert report["overall"]["status"] == "pass"
    assert not data_dir.exists()
    assert _snapshot(root) == before


def test_json_report_is_stable_across_runs(tmp_path):
    root = make_checkout(tmp_path / "repo")
    first = json.dumps(_report(root, tmp_path), indent=2, sort_keys=True)
    second = json.dumps(_report(root, tmp_path), indent=2, sort_keys=True)
    assert first == second


# ---------------------------------------------------------------------------
# Installed-distribution fixtures
# ---------------------------------------------------------------------------


def _make_site(site: Path, *, with_mcp: bool = True) -> Path:
    _write(
        site / "scholialang-0.7.3.dist-info/METADATA",
        "Metadata-Version: 2.1\nName: scholialang\nVersion: 0.7.3\n",
    )
    if with_mcp:
        _write(
            site / "scholialang_mcp-0.7.3.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: scholialang-mcp\nVersion: 0.7.3\n",
        )
        _write(
            site / "scholialang_mcp-0.7.3.dist-info/entry_points.txt",
            "[console_scripts]\n"
            "scholialang-mcp = scholialang_mcp.server:main\n"
            "scholialang-lsp = scholialang_mcp.lsp.server:main\n",
        )
    return site


def test_installed_mode_reads_distribution_metadata_from_explicit_site(tmp_path):
    site = _make_site(tmp_path / "site")
    report = _report(
        tmp_path / "not-a-checkout",
        tmp_path,
        mode="installed",
        search_path=[str(site)],
        project=tmp_path / "not-a-checkout",
    )

    facets = report["facets"]
    assert facets["python_package"]["version"] == RELEASE
    assert facets["mcp_package"]["version"] == RELEASE
    assert facets["mcp_entry_point"]["present"] is True
    assert facets["lsp_entry_point"]["present"] is True
    assert facets["plugin"]["supported"] is False
    assert report["overall"]["status"] == "pass"


def test_installed_mode_missing_mcp_distribution_is_specific_not_ready(tmp_path):
    site = _make_site(tmp_path / "site", with_mcp=False)
    report = _report(
        tmp_path / "not-a-checkout",
        tmp_path,
        mode="installed",
        search_path=[str(site)],
        project=tmp_path / "not-a-checkout",
    )

    assert report["overall"]["status"] == "not_ready"
    assert ("mcp_package", "not_ready") in _reasons(report)
    assert ("mcp_entry_point", "not_ready") in _reasons(report)
    assert "not installed" in _messages(report)


# ---------------------------------------------------------------------------
# CLI behavior: hostile CWD, redaction, exit codes
# ---------------------------------------------------------------------------


def _run_doctor(args, *, cwd: Path, extra_env: dict[str, str] | None = None):
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(cwd),
    }
    env.update(extra_env or {})
    return subprocess.run(
        [sys.executable, str(DOCTOR_SCRIPT), *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_hostile_cwd_code_is_never_imported(tmp_path):
    root = make_checkout(tmp_path / "repo")
    hostile = tmp_path / "hostile-cwd"
    canary = hostile / "canary.txt"
    payload = f"open({str(canary)!r}, 'w').write('boom')\n"
    _write(hostile / "tomllib.py", payload)
    _write(hostile / "scholialang_mcp/__init__.py", payload)
    _write(hostile / "json.py", payload)

    result = _run_doctor(
        ["--mode", "repo", "--root", str(root), "--data-dir", str(tmp_path / "d"), "--json"],
        cwd=hostile,
    )

    assert result.returncode == 0, result.stderr
    assert not canary.exists(), "doctor imported code from the untrusted CWD"
    assert json.loads(result.stdout)["overall"]["status"] == "pass"


def test_report_never_contains_environment_secret_values(tmp_path):
    root = make_checkout(tmp_path / "repo")
    result = _run_doctor(
        ["--mode", "repo", "--root", str(root), "--data-dir", str(tmp_path / "d"), "--json"],
        cwd=root,
        extra_env={
            "EXAMPLE_API_TOKEN": "hunter2-canary-value",
            "SCHOLIA_AUTOEMIT": "1",
        },
    )

    assert result.returncode == 0, result.stderr
    assert "hunter2-canary-value" not in result.stdout
    assert "hunter2-canary-value" not in result.stderr
    report = json.loads(result.stdout)
    # The one named variable the doctor may read is reported as a normalized
    # state, never echoed back as a raw value.
    assert report["facets"]["auto_emit"]["state"] == "enabled"


def test_exit_codes_track_overall_status(tmp_path):
    healthy = make_checkout(tmp_path / "healthy")
    stale = make_checkout(tmp_path / "stale", skill_version="0.7.1")
    broken = make_checkout(tmp_path / "broken", plugin_versions={"codex": "0.6.0"})
    common = ["--data-dir", str(tmp_path / "d"), "--json"]

    assert _run_doctor(["--mode", "repo", "--root", str(healthy), *common], cwd=healthy).returncode == 0
    assert _run_doctor(["--mode", "repo", "--root", str(stale), *common], cwd=stale).returncode == 1
    assert _run_doctor(["--mode", "repo", "--root", str(broken), *common], cwd=broken).returncode == 2


# ---------------------------------------------------------------------------
# The real checkout and the shipped skill
# ---------------------------------------------------------------------------


def test_real_checkout_reports_pass_with_release_and_grammar_axes(tmp_path):
    report = _report(ROOT, tmp_path, project=ROOT)

    assert report["overall"] == {"status": "pass", "reasons": []}
    facets = report["facets"]
    assert facets["grammar"]["version"] == GRAMMAR
    assert facets["mcp_package"]["version"] == RELEASE
    assert facets["skill"]["version"] == RELEASE
    assert facets["skill"]["host_copies_identical"] is True


def test_shipped_skill_is_concise_and_has_no_auxiliary_files():
    forbidden = {"README.md", "INSTALLATION_GUIDE.md", "CHANGELOG.md"}
    for skill_dir in (CANONICAL_SKILL, CODEX_SKILL):
        names = {path.name for path in skill_dir.rglob("*") if path.is_file()}
        assert not names & forbidden, f"auxiliary docs found in {skill_dir}"
        assert "SKILL.md" in names

    frontmatter = doctor._skill_frontmatter((CANONICAL_SKILL / "SKILL.md").read_text("utf-8"))
    assert frontmatter["version"] == RELEASE
    assert frontmatter["grammar"] == GRAMMAR


def test_codex_host_copy_is_byte_identical_generated_artifact():
    assert doctor._skill_copies_identical(CANONICAL_SKILL, CODEX_SKILL), (
        "Codex host copy drifted from the canonical skill; run scripts/sync_plugins.sh"
    )


def test_skill_frontmatter_uses_only_allowed_keys():
    allowed = {"name", "description", "license", "allowed-tools", "metadata", "compatibility"}
    text = (CANONICAL_SKILL / "SKILL.md").read_text(encoding="utf-8")
    match = doctor._FRONTMATTER_RE.match(text)
    assert match is not None
    top_level_keys = {
        line.split(":", 1)[0].strip()
        for line in match.group(1).splitlines()
        if line and not line.startswith((" ", "\t")) and ":" in line
    }
    assert top_level_keys <= allowed
    assert {"name", "description"} <= top_level_keys
