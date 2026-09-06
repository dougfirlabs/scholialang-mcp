#!/usr/bin/env python3
"""Read-only Scholialang version and capability doctor.

Reports, as separate named axes, what is loaded on this host — language
grammar, Python package, MCP package, plugin manifests, vendored validator,
skill copies, MCP/LSP entry points, auto-emit state, and database
reachability metadata — and derives an honest overall ``pass`` /
``not_ready`` / ``fail`` with per-facet reasons.

Guarantees:

* Read-only. Never installs, upgrades, authenticates, edits configuration,
  initializes a DAG, or repairs a database. Fixes are surfaced only as
  ``recommendations`` strings.
* Probes only public metadata and explicitly named local paths. Reads a
  single environment variable (``SCHOLIA_AUTOEMIT``) and reports it as a
  normalized state, never a raw value. Never opens the trace database.
* Never imports project or installed Scholialang code. Repository probes
  parse files (``tomllib`` / ``ast`` / ``json``); installed probes read
  distribution metadata via ``importlib.metadata``. The current working
  directory is stripped from ``sys.path`` before any probe runs.
"""

from __future__ import annotations

import argparse
import ast
import json
import os
import re
import shutil
import sys
import tomllib
from importlib.metadata import Distribution
from pathlib import Path

DOCTOR_VERSION = "0.7.3"
SCHEMA_VERSION = 1
# The stable Scholia language grammar. The implementation release (package,
# plugin, vendored validator) is a DISTINCT axis: release 0.7.3 implements
# grammar v0.6.2. That relationship is expected alignment, never a downgrade.
STABLE_GRAMMAR_VERSION = "0.6.2"

PYTHON_PACKAGE_DIST = "scholialang"
MCP_PACKAGE_DIST = "scholialang-mcp"
CONSOLE_SCRIPTS = ("scholialang-mcp", "scholialang-lsp")
VENDOR_HOSTS = ("claude-code", "codex", "ollama")
SKILL_NAME = "scholialang-doctor"
CANONICAL_SKILL_DIR = Path("plugins/claude-code/scholialang/skills") / SKILL_NAME
CODEX_SKILL_DIR = Path("plugins/codex/scholialang/skills") / SKILL_NAME

AUTOEMIT_ENV = "SCHOLIA_AUTOEMIT"
AUTOEMIT_OFF_VALUES = {"0", "false", "off"}
OPT_OUT_FILE = ".scholia-off"
DB_FILENAME = "scholialang.sqlite3"

_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---", re.DOTALL)
_META_VERSION_RE = re.compile(r'^\s+version:\s*"?([0-9][\w.\-]*)"?\s*$', re.MULTILINE)
_META_GRAMMAR_RE = re.compile(r'^\s+grammar:\s*"?([0-9][\w.\-]*)"?\s*$', re.MULTILINE)


def _harden_sys_path() -> None:
    """Drop the CWD from module/metadata search so an untrusted working
    directory can never shadow the artifact being inspected."""
    cwd = os.getcwd()
    sys.path[:] = [
        entry
        for entry in sys.path
        if entry not in ("", ".") and os.path.realpath(entry or ".") != os.path.realpath(cwd)
    ]


def _facet(supported: bool, present: bool, *, version=None, compatible=None, detail=None, **extra):
    data = {
        "supported": bool(supported),
        "present": bool(present),
        "version": version,
        "compatible": compatible,
        "detail": detail,
    }
    data.update(extra)
    return data


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _read_text(path: Path):
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return None


def _ast_constant(path: Path, name: str):
    text = _read_text(path)
    if text is None:
        return None
    try:
        tree = ast.parse(text, filename=str(path))
    except SyntaxError:
        return None
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == name:
                    try:
                        return ast.literal_eval(node.value)
                    except ValueError:
                        return None
    return None


def _read_pyproject(root: Path):
    text = _read_text(root / "pyproject.toml")
    if text is None:
        return None
    try:
        return tomllib.loads(text).get("project")
    except tomllib.TOMLDecodeError:
        return None


def is_checkout(root: Path) -> bool:
    project = _read_pyproject(root)
    return bool(project) and project.get("name") == MCP_PACKAGE_DIST


def _skill_frontmatter(skill_md_text: str):
    match = _FRONTMATTER_RE.match(skill_md_text or "")
    if not match:
        return {"version": None, "grammar": None}
    block = match.group(1)
    version = _META_VERSION_RE.search(block)
    grammar = _META_GRAMMAR_RE.search(block)
    return {
        "version": version.group(1) if version else None,
        "grammar": grammar.group(1) if grammar else None,
    }


def _skill_files(skill_dir: Path) -> list[Path]:
    return sorted(
        path.relative_to(skill_dir)
        for path in skill_dir.rglob("*")
        if path.is_file()
        and "__pycache__" not in path.relative_to(skill_dir).parts
        and path.suffix != ".pyc"
    )


def _skill_copies_identical(canonical: Path, copy: Path) -> bool:
    if not copy.is_dir():
        return False
    canonical_files = _skill_files(canonical)
    copy_files = _skill_files(copy)
    if canonical_files != copy_files:
        return False
    return all(
        (canonical / rel).read_bytes() == (copy / rel).read_bytes() for rel in canonical_files
    )


# ---------------------------------------------------------------------------
# Repository-checkout probes
# ---------------------------------------------------------------------------


def _repo_facets(root: Path) -> dict:
    facets: dict[str, dict] = {}
    project = _read_pyproject(root)
    declared_scripts = (project or {}).get("scripts", {})

    pyproject_version = (project or {}).get("version")
    init_version = _ast_constant(root / "src" / "scholialang_mcp" / "__init__.py", "__version__")
    facets["mcp_package"] = _facet(
        True,
        project is not None,
        version=pyproject_version,
        compatible=(
            None
            if pyproject_version is None or init_version is None
            else pyproject_version == init_version
        ),
        detail="pyproject.toml project.version vs src package __version__",
        source="checkout",
        package_version=init_version,
    )

    dependency = next(
        (item for item in (project or {}).get("dependencies", []) if "scholialang" in item),
        None,
    )
    facets["python_package"] = _facet(
        True,
        dependency is not None,
        version=None,
        compatible=None,
        detail="declared scholialang dependency of the checkout (not an installed distribution)",
        source="checkout",
        requirement=dependency,
    )

    plugin_versions = {
        "claude-code": (
            _read_json(root / "plugins/claude-code/scholialang/.claude-plugin/plugin.json") or {}
        ).get("version"),
        "codex": (
            _read_json(root / "plugins/codex/scholialang/.codex-plugin/plugin.json") or {}
        ).get("version"),
        "claude-desktop": (
            _read_json(root / "plugins/claude-desktop/scholialang/manifest.json") or {}
        ).get("version"),
    }
    present_plugin_versions = {v for v in plugin_versions.values() if v is not None}
    facets["plugin"] = _facet(
        True,
        bool(present_plugin_versions),
        version=(
            next(iter(present_plugin_versions)) if len(present_plugin_versions) == 1 else None
        ),
        compatible=(
            None
            if not present_plugin_versions or pyproject_version is None
            else present_plugin_versions == {pyproject_version}
        ),
        detail="host plugin manifest versions",
        versions=plugin_versions,
    )

    vendored_versions = {
        host: (
            _read_json(
                root
                / "plugins"
                / host
                / "scholialang"
                / "scripts"
                / "_scholia_vendored"
                / "UPSTREAM.json"
            )
            or {}
        ).get("validator_version")
        for host in VENDOR_HOSTS
    }
    present_vendored = {v for v in vendored_versions.values() if v is not None}
    facets["vendored_validator"] = _facet(
        True,
        bool(present_vendored),
        version=next(iter(present_vendored)) if len(present_vendored) == 1 else None,
        compatible=len(present_vendored) == 1 if present_vendored else None,
        detail="vendored validator engine version per host (must be in parity)",
        versions=vendored_versions,
    )

    spec_upstream = _read_json(
        root / "tests" / "fixtures" / "scholialang-spec" / f"v{STABLE_GRAMMAR_VERSION}" / "UPSTREAM.json"
    )
    corpus_version = (spec_upstream or {}).get("spec_version")
    facets["grammar"] = _facet(
        True,
        corpus_version is not None,
        version=corpus_version or STABLE_GRAMMAR_VERSION,
        compatible=None if corpus_version is None else corpus_version == STABLE_GRAMMAR_VERSION,
        detail=(
            f"stable Scholia language grammar; release {pyproject_version or 'unknown'} "
            f"implements grammar v{STABLE_GRAMMAR_VERSION} (distinct axes, not a downgrade)"
        ),
        source="spec conformance corpus",
    )

    canonical_dir = root / CANONICAL_SKILL_DIR
    canonical_md = _read_text(canonical_dir / "SKILL.md")
    skill_meta = _skill_frontmatter(canonical_md or "")
    copies_ok = (
        _skill_copies_identical(canonical_dir, root / CODEX_SKILL_DIR)
        if canonical_md is not None
        else False
    )
    version_ok = (
        None
        if skill_meta["version"] is None or pyproject_version is None
        else skill_meta["version"] == pyproject_version
    )
    facets["skill"] = _facet(
        True,
        canonical_md is not None,
        version=skill_meta["version"],
        compatible=None if canonical_md is None else bool(copies_ok and version_ok is not False),
        detail="canonical doctor skill and generated host copies",
        grammar=skill_meta["grammar"],
        host_copies_identical=copies_ok,
        matches_release=version_ok,
    )

    facets["mcp_entry_point"] = _facet(
        True,
        "scholialang-mcp" in declared_scripts
        and (root / "src" / "scholialang_mcp" / "server.py").is_file(),
        version=None,
        compatible=None,
        detail="console script scholialang-mcp declared and server module present",
        plugin_server=(
            root / "plugins/claude-code/scholialang/scripts/scholialang_mcp_server.py"
        ).is_file(),
    )
    facets["lsp_entry_point"] = _facet(
        True,
        "scholialang-lsp" in declared_scripts
        and (root / "src" / "scholialang_mcp" / "lsp" / "server.py").is_file(),
        version=None,
        compatible=None,
        detail="console script scholialang-lsp declared and LSP server module present",
    )
    return facets


# ---------------------------------------------------------------------------
# Installed-distribution probes
# ---------------------------------------------------------------------------


def _find_distribution(name: str, search_path):
    try:
        return next(iter(Distribution.discover(name=name, path=search_path)), None)
    except Exception:
        return None


def _installed_facets(search_path) -> dict:
    facets: dict[str, dict] = {}

    scholia_dist = _find_distribution(PYTHON_PACKAGE_DIST, search_path)
    facets["python_package"] = _facet(
        True,
        scholia_dist is not None,
        version=scholia_dist.version if scholia_dist else None,
        compatible=None,
        detail="installed scholialang distribution metadata",
        source="installed",
    )

    mcp_dist = _find_distribution(MCP_PACKAGE_DIST, search_path)
    entry_names = set()
    if mcp_dist is not None:
        entry_names = {
            entry.name for entry in mcp_dist.entry_points if entry.group == "console_scripts"
        }
    facets["mcp_package"] = _facet(
        True,
        mcp_dist is not None,
        version=mcp_dist.version if mcp_dist else None,
        compatible=None,
        detail="installed scholialang-mcp distribution metadata",
        source="installed",
    )

    for facet_name, script in zip(("mcp_entry_point", "lsp_entry_point"), CONSOLE_SCRIPTS):
        facets[facet_name] = _facet(
            True,
            script in entry_names,
            version=None,
            compatible=None,
            detail=f"console script {script} declared by the installed distribution",
            on_path=shutil.which(script) is not None,
        )

    for facet_name, detail in (
        ("plugin", "host plugin manifests are repository surfaces"),
        ("vendored_validator", "vendored validator snapshots are repository surfaces"),
        ("skill", "skill sources are repository surfaces"),
    ):
        facets[facet_name] = _facet(
            False,
            False,
            version=None,
            compatible=None,
            detail=f"{detail}; not probed in installed mode",
        )

    facets["grammar"] = _facet(
        True,
        True,
        version=STABLE_GRAMMAR_VERSION,
        compatible=True,
        detail=(
            f"stable Scholia language grammar targeted by the doctor contract; the "
            f"installed release implements grammar v{STABLE_GRAMMAR_VERSION} "
            "(distinct axes, not a downgrade)"
        ),
        source="doctor contract",
    )
    return facets


# ---------------------------------------------------------------------------
# Host-environment probes (both modes)
# ---------------------------------------------------------------------------


def _auto_emit_facet(project_root: Path) -> dict:
    raw = os.environ.get(AUTOEMIT_ENV)
    opt_out_file = (project_root / OPT_OUT_FILE).exists()
    if opt_out_file:
        state, source = "disabled", f"{OPT_OUT_FILE} file in project root"
    elif raw is not None and raw.strip().lower() in AUTOEMIT_OFF_VALUES:
        state, source = "disabled", f"{AUTOEMIT_ENV} environment opt-out"
    elif raw is not None:
        state, source = "enabled", f"{AUTOEMIT_ENV} environment setting"
    else:
        state, source = "enabled", "default (no opt-out found)"
    return _facet(
        True,
        True,
        version=None,
        compatible=state == "enabled",
        detail="auto-emit configuration (normalized state only; raw values are never reported)",
        state=state,
        source=source,
    )


def _database_facet(data_dir: Path) -> dict:
    db_path = data_dir / DB_FILENAME
    exists = db_path.is_file()
    size = db_path.stat().st_size if exists else None
    return _facet(
        True,
        exists,
        version=None,
        compatible=None,
        detail=(
            "trace database reachability metadata only; the doctor never opens the "
            "database or reads DAG contents"
        ),
        path=str(db_path),
        size_bytes=size,
        readable=os.access(db_path, os.R_OK) if exists else None,
    )


# ---------------------------------------------------------------------------
# Overall derivation
# ---------------------------------------------------------------------------


def _derive_overall(mode: str, facets: dict) -> tuple[dict, list[str]]:
    reasons: list[dict] = []
    recommendations: list[str] = []

    def flag(facet_name: str, severity: str, message: str, recommendation: str | None = None):
        reasons.append({"facet": facet_name, "severity": severity, "message": message})
        if recommendation:
            recommendations.append(recommendation)

    mcp_package = facets["mcp_package"]
    if mode == "repo":
        if not mcp_package["present"]:
            flag(
                "mcp_package",
                "fail",
                "selected root is not a readable scholialang-mcp checkout",
                "Point --root at a scholialang-mcp checkout or use --mode installed.",
            )
        elif mcp_package["compatible"] is False:
            flag(
                "mcp_package",
                "fail",
                f"pyproject version {mcp_package['version']} != package __version__ "
                f"{mcp_package['package_version']}",
                "Align pyproject.toml and src/scholialang_mcp/__init__.py via the release process.",
            )

        plugin = facets["plugin"]
        missing_plugins = sorted(
            host for host, version in plugin.get("versions", {}).items() if version is None
        )
        if missing_plugins:
            flag(
                "plugin",
                "not_ready",
                f"plugin manifest missing or unreadable for: {', '.join(missing_plugins)}",
                "Restore the missing plugin manifest(s) from the checkout history.",
            )
        if plugin["compatible"] is False:
            flag(
                "plugin",
                "fail",
                f"plugin manifest versions {plugin.get('versions')} do not all match "
                f"package release {mcp_package['version']}",
                "Re-align plugin manifests with the package release; the doctor never edits them.",
            )

        vendored = facets["vendored_validator"]
        if not vendored["present"]:
            flag(
                "vendored_validator",
                "not_ready",
                "no vendored validator UPSTREAM.json found",
                "Re-vendor the validator with scripts/vendor_scholialang.py (manual step).",
            )
        elif vendored["compatible"] is False:
            flag(
                "vendored_validator",
                "fail",
                f"vendored validator versions out of parity: {vendored.get('versions')}",
                "Run scripts/sync_plugins.sh to restore host parity (manual step).",
            )

        grammar = facets["grammar"]
        if not grammar["present"]:
            flag(
                "grammar",
                "not_ready",
                "spec conformance corpus UPSTREAM.json not found in the checkout",
            )
        elif grammar["compatible"] is False:
            flag(
                "grammar",
                "fail",
                f"spec corpus reports grammar {grammar['version']}, doctor contract expects "
                f"v{STABLE_GRAMMAR_VERSION}",
            )

        skill = facets["skill"]
        if not skill["present"]:
            flag(
                "skill",
                "not_ready",
                f"canonical {SKILL_NAME} skill not found in the checkout",
            )
        else:
            if skill.get("matches_release") is False:
                flag(
                    "skill",
                    "not_ready",
                    f"skill is stale: skill version {skill['version']} != package release "
                    f"{mcp_package['version']}",
                    "Refresh the canonical skill as part of the release process.",
                )
            if not skill.get("host_copies_identical"):
                flag(
                    "skill",
                    "not_ready",
                    "generated codex host copy of the skill is missing or stale",
                    "Run scripts/sync_plugins.sh to regenerate host copies (manual step).",
                )
    else:
        python_package = facets["python_package"]
        if not python_package["present"]:
            flag(
                "python_package",
                "not_ready",
                "scholialang distribution is not installed",
                "Install the scholialang distribution (manual step; the doctor never installs).",
            )
        if not mcp_package["present"]:
            flag(
                "mcp_package",
                "not_ready",
                "scholialang-mcp distribution is not installed",
                "Install the scholialang-mcp distribution (manual step; the doctor never installs).",
            )

    for facet_name, label in (("mcp_entry_point", "MCP"), ("lsp_entry_point", "LSP")):
        if not facets[facet_name]["present"]:
            flag(
                facet_name,
                "not_ready",
                f"{label} entry point is not available on this surface",
            )

    auto_emit = facets["auto_emit"]
    if auto_emit["state"] == "disabled":
        flag(
            "auto_emit",
            "not_ready",
            f"auto-emit is opted out ({auto_emit['source']})",
            "Remove the opt-out to re-enable auto-emit; the doctor never edits configuration.",
        )

    # The database facet is reachability metadata only: an absent local
    # database is a normal state for a fresh host, never a failure.

    if any(reason["severity"] == "fail" for reason in reasons):
        status = "fail"
    elif reasons:
        status = "not_ready"
    else:
        status = "pass"
    return {"status": status, "reasons": reasons}, recommendations


def _compatibility_facet(mode: str, facets: dict, overall: dict) -> dict:
    release = facets["mcp_package"]["version"]
    return _facet(
        True,
        True,
        version=None,
        compatible=overall["status"] != "fail",
        detail=(
            f"cross-axis summary: release {release or 'unknown'} implements the stable "
            f"Scholia grammar v{STABLE_GRAMMAR_VERSION}; grammar and release are distinct "
            "version axes and their difference is expected alignment, not a downgrade"
        ),
        mode=mode,
        grammar_version=STABLE_GRAMMAR_VERSION,
        release_version=release,
    )


# ---------------------------------------------------------------------------
# Report assembly and CLI
# ---------------------------------------------------------------------------


def build_report(
    *,
    mode: str = "auto",
    root: Path | None = None,
    project: Path | None = None,
    data_dir: Path | None = None,
    search_path=None,
) -> dict:
    root = Path(root) if root is not None else Path.cwd()
    if mode == "auto":
        mode = "repo" if is_checkout(root) else "installed"
    project = Path(project) if project is not None else root
    data_dir = Path(data_dir) if data_dir is not None else Path.home() / ".scholialang"

    if mode == "repo":
        facets = _repo_facets(root)
    else:
        facets = _installed_facets(list(search_path) if search_path else None)
    facets["auto_emit"] = _auto_emit_facet(project)
    facets["database"] = _database_facet(data_dir)

    overall, recommendations = _derive_overall(mode, facets)
    facets["compatibility"] = _compatibility_facet(mode, facets, overall)

    return {
        "doctor": {
            "name": SKILL_NAME,
            "version": DOCTOR_VERSION,
            "schema_version": SCHEMA_VERSION,
            "mode": mode,
            "read_only": True,
        },
        "facets": facets,
        "overall": overall,
        "recommendations": recommendations,
    }


def _render_human(report: dict) -> str:
    lines = [
        f"{SKILL_NAME} {DOCTOR_VERSION} (mode={report['doctor']['mode']}, read-only)",
    ]
    for name in sorted(report["facets"]):
        data = report["facets"][name]
        if not data["supported"]:
            state = "n/a"
        elif not data["present"]:
            state = "absent"
        elif data["compatible"] is False:
            state = "mismatch"
        else:
            state = "ok"
        version = data["version"] or "-"
        lines.append(f"  {name:<20} {state:<8} {version:<8} {data['detail']}")
    overall = report["overall"]
    for reason in overall["reasons"]:
        lines.append(f"  ! [{reason['severity']}] {reason['facet']}: {reason['message']}")
    for recommendation in report["recommendations"]:
        lines.append(f"  > recommendation: {recommendation}")
    lines.append(f"overall: {overall['status']}")
    return "\n".join(lines)


def main(argv=None) -> int:
    _harden_sys_path()
    parser = argparse.ArgumentParser(
        prog=SKILL_NAME,
        description="Read-only Scholialang version and capability doctor.",
    )
    parser.add_argument(
        "--mode",
        choices=("auto", "repo", "installed"),
        default="auto",
        help="repo inspects a checkout; installed inspects distribution metadata",
    )
    parser.add_argument("--root", type=Path, default=None, help="checkout root for repo mode")
    parser.add_argument(
        "--project", type=Path, default=None, help="project root for the auto-emit opt-out probe"
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="Scholialang data directory (default: ~/.scholialang)",
    )
    parser.add_argument(
        "--site",
        action="append",
        default=None,
        help="explicit distribution metadata search path for installed mode (repeatable)",
    )
    parser.add_argument("--json", action="store_true", help="emit the stable JSON report")
    args = parser.parse_args(argv)

    report = build_report(
        mode=args.mode,
        root=args.root,
        project=args.project,
        data_dir=args.data_dir,
        search_path=args.site,
    )
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(_render_human(report))
    return {"pass": 0, "not_ready": 1, "fail": 2}[report["overall"]["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
