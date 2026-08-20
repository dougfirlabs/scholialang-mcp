#!/usr/bin/env python3
"""Deterministic canonical-to-host materializer for the public skills.

The two public skills (``scholialang-doctor``, ``scholialang-verify``) have
exactly one canonical source each, under
``plugins/claude-code/scholialang/skills/<skill>/``: the ``SKILL.md`` body and
every bundled ``scripts/`` file are authored there and nowhere else. This
script renders the canonical source into every supported host surface:

* **claude-code** — the canonical tree itself, plus the *generated* Codex UI
  metadata file ``agents/openai.yaml`` (kept in the canonical tree so host
  copies stay byte-identical).
* **codex** — a byte-identical generated copy of the final skill tree,
  recorded in a ``PROVENANCE.json`` manifest next to the copies.
* **claude-desktop / ollama** — no native skill surface. These hosts receive
  the corresponding MCP capabilities through the ``.mcpb`` bundle
  (``scripts/build_claude_desktop_mcpb.py``) and the server recipes; the
  matrix below reports ``native_skill_surface: false`` honestly instead of
  pretending an MCP bundle is a skill installation.

Guarantees:

* Deterministic: rendering the same source twice yields byte-identical
  outputs — no timestamps, no environment-dependent content, sorted file
  order everywhere.
* ``agents/openai.yaml`` is generated from the final skill content (the
  frontmatter ``name``) plus one explicit host overlay string; a manual edit
  of a generated file is detected by ``--check``.
* ``--check`` re-renders from the canonical source into memory and fails on
  any stale, manually edited, missing, or extra generated file, and on any
  metadata / trigger / reference / executable-mode violation.
* Write mode touches only the generated outputs listed in the provenance
  manifest; it never edits canonical ``SKILL.md`` bodies or bundled scripts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path

MATERIALIZER_VERSION = "0.7.2"
SCHEMA_VERSION = 1
STABLE_GRAMMAR_VERSION = "0.6.2"

SKILLS = ("scholialang-doctor", "scholialang-verify")
CANONICAL_SKILLS_DIR = Path("plugins/claude-code/scholialang/skills")
CODEX_SKILLS_DIR = Path("plugins/codex/scholialang/skills")
PROVENANCE_NAME = "PROVENANCE.json"
OPENAI_YAML = Path("agents/openai.yaml")

# Host-specific wording is allowed only as an explicit overlay; everything
# else in a generated file derives from the canonical skill content.
CODEX_SHORT_DESCRIPTIONS = {
    "scholialang-doctor": "Read-only Scholialang version and capability doctor",
    "scholialang-verify": "Isolated verification of the public Scholialang contract",
}

# Every supported host, with an honest statement of its skill surface. A host
# without a native skill surface is never claimed to carry the skill; it is
# covered by the MCP capability bundle instead.
HOST_MATRIX = (
    {
        "host": "claude-code",
        "native_skill_surface": True,
        "role": "canonical",
        "delivery": "plugin skills directory (canonical source)",
    },
    {
        "host": "codex",
        "native_skill_surface": True,
        "role": "generated",
        "delivery": "plugin skills directory (generated copy with provenance)",
    },
    {
        "host": "claude-desktop",
        "native_skill_surface": False,
        "role": "mcp_bundle",
        "delivery": (
            ".mcpb MCP server bundle (scripts/build_claude_desktop_mcpb.py); "
            "SKILL.md is not loaded by this host"
        ),
    },
    {
        "host": "ollama",
        "native_skill_surface": False,
        "role": "mcp_only",
        "delivery": "MCP server + integration recipes; no skill files shipped",
    },
)

FORBIDDEN_AUX_FILES = {"README.md", "INSTALLATION_GUIDE.md", "CHANGELOG.md"}
ALLOWED_FRONTMATTER_KEYS = {
    "name",
    "description",
    "license",
    "allowed-tools",
    "metadata",
    "compatibility",
}
REQUIRED_FRONTMATTER_KEYS = {"name", "description"}
TRIGGER_PREFIX = "Use when"
TRIGGER_MIN_LEN = 80
TRIGGER_MAX_LEN = 1024

_FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---", re.DOTALL)
_META_VERSION_RE = re.compile(r'^\s+version:\s*"?([0-9][\w.\-]*)"?\s*$', re.MULTILINE)
_META_GRAMMAR_RE = re.compile(r'^\s+grammar:\s*"?([0-9][\w.\-]*)"?\s*$', re.MULTILINE)
_NAME_RE = re.compile(r"^name:\s*(\S+)\s*$", re.MULTILINE)
_DESCRIPTION_RE = re.compile(r"^description:\s*(.+)$", re.MULTILINE)
_SCRIPT_REF_RE = re.compile(r"\bscripts/[\w.\-]+\.py\b")
_ABSOLUTE_PATH_RE = re.compile(r"(?<![\w:])/(?:home|Users|root)/[\w./\-]+")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _display_name(skill_name: str) -> str:
    return " ".join(part.capitalize() for part in skill_name.split("-"))


def _frontmatter_block(skill_md_text: str) -> str | None:
    match = _FRONTMATTER_RE.match(skill_md_text or "")
    return match.group(1) if match else None


def _top_level_keys(block: str) -> set[str]:
    return {
        line.split(":", 1)[0].strip()
        for line in block.splitlines()
        if line and not line.startswith((" ", "\t")) and ":" in line
    }


def _skill_files(skill_dir: Path) -> list[Path]:
    return sorted(
        path.relative_to(skill_dir)
        for path in skill_dir.rglob("*")
        if path.is_file()
        and "__pycache__" not in path.relative_to(skill_dir).parts
        and path.suffix != ".pyc"
    )


# ---------------------------------------------------------------------------
# Rendering: canonical source -> final host tree, in memory
# ---------------------------------------------------------------------------


def render_openai_yaml(skill_name: str, skill_md_text: str) -> bytes:
    """Codex UI metadata, generated from the final skill content.

    The display name derives mechanically from the frontmatter ``name``; the
    short description is the one explicit host-overlay string.
    """
    block = _frontmatter_block(skill_md_text) or ""
    name_match = _NAME_RE.search(block)
    name = name_match.group(1) if name_match else skill_name
    short = CODEX_SHORT_DESCRIPTIONS[skill_name]
    return (
        "interface:\n"
        f'  display_name: "{_display_name(name)}"\n'
        f'  short_description: "{short}"\n'
    ).encode("utf-8")


def render_skill_tree(root: Path, skill_name: str) -> dict[str, bytes]:
    """The final per-host skill tree as ``{relative_path: bytes}``.

    Canonical authored files are passed through byte-for-byte;
    ``agents/openai.yaml`` is always regenerated from the skill content.
    """
    canonical_dir = root / CANONICAL_SKILLS_DIR / skill_name
    rendered: dict[str, bytes] = {}
    for rel in _skill_files(canonical_dir):
        if rel == OPENAI_YAML:
            continue
        rendered[rel.as_posix()] = (canonical_dir / rel).read_bytes()
    skill_md = rendered.get("SKILL.md", b"").decode("utf-8", errors="replace")
    rendered[OPENAI_YAML.as_posix()] = render_openai_yaml(skill_name, skill_md)
    return dict(sorted(rendered.items()))


def render_provenance(trees: dict[str, dict[str, bytes]]) -> bytes:
    manifest = {
        "schema_version": SCHEMA_VERSION,
        "generated_by": "scripts/materialize_skills.py",
        "generator_version": MATERIALIZER_VERSION,
        "grammar_version": STABLE_GRAMMAR_VERSION,
        "canonical_source": CANONICAL_SKILLS_DIR.as_posix(),
        "do_not_edit": True,
        "regenerate": "python3 scripts/materialize_skills.py --root <checkout>",
        "skills": {
            skill: {"files": {rel: _sha256(data) for rel, data in tree.items()}}
            for skill, tree in trees.items()
        },
    }
    return (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode("utf-8")


def render_all(root: Path) -> tuple[dict[str, dict[str, bytes]], bytes]:
    trees = {skill: render_skill_tree(root, skill) for skill in SKILLS}
    return trees, render_provenance(trees)


# ---------------------------------------------------------------------------
# Validation: metadata, triggers, references, executable modes
# ---------------------------------------------------------------------------


def _validate_frontmatter(skill_name: str, skill_md_text: str) -> list[str]:
    issues: list[str] = []
    block = _frontmatter_block(skill_md_text)
    if block is None:
        return [f"{skill_name}: SKILL.md has no frontmatter block"]
    keys = _top_level_keys(block)
    if not REQUIRED_FRONTMATTER_KEYS <= keys:
        issues.append(
            f"{skill_name}: frontmatter missing required keys "
            f"{sorted(REQUIRED_FRONTMATTER_KEYS - keys)}"
        )
    if not keys <= ALLOWED_FRONTMATTER_KEYS:
        issues.append(
            f"{skill_name}: frontmatter has disallowed keys "
            f"{sorted(keys - ALLOWED_FRONTMATTER_KEYS)}"
        )
    name_match = _NAME_RE.search(block)
    if not name_match or name_match.group(1) != skill_name:
        issues.append(f"{skill_name}: frontmatter name does not match the skill directory")
    version = _META_VERSION_RE.search(block)
    if not version or version.group(1) != MATERIALIZER_VERSION:
        issues.append(
            f"{skill_name}: metadata.version must be {MATERIALIZER_VERSION!r}"
        )
    grammar = _META_GRAMMAR_RE.search(block)
    if not grammar or grammar.group(1) != STABLE_GRAMMAR_VERSION:
        issues.append(
            f"{skill_name}: metadata.grammar must be {STABLE_GRAMMAR_VERSION!r}"
        )
    description = _DESCRIPTION_RE.search(block)
    if description is None:
        issues.append(f"{skill_name}: frontmatter has no description")
    else:
        trigger = description.group(1).strip()
        if not trigger.startswith(TRIGGER_PREFIX):
            issues.append(
                f"{skill_name}: trigger description must start with {TRIGGER_PREFIX!r}"
            )
        if not TRIGGER_MIN_LEN <= len(trigger) <= TRIGGER_MAX_LEN:
            issues.append(
                f"{skill_name}: trigger description length {len(trigger)} outside "
                f"[{TRIGGER_MIN_LEN}, {TRIGGER_MAX_LEN}]"
            )
    return issues


def _validate_references(skill_name: str, tree: dict[str, bytes]) -> list[str]:
    issues: list[str] = []
    body = tree.get("SKILL.md", b"").decode("utf-8", errors="replace")
    for ref in sorted(set(_SCRIPT_REF_RE.findall(body))):
        if ref not in tree:
            issues.append(f"{skill_name}: SKILL.md references missing file {ref}")
    for match in _ABSOLUTE_PATH_RE.findall(body):
        issues.append(f"{skill_name}: SKILL.md contains an absolute path {match!r}")
    if "../" in body:
        issues.append(f"{skill_name}: SKILL.md escapes the skill directory with '../'")
    return issues


def _validate_tree_shape(skill_name: str, tree: dict[str, bytes]) -> list[str]:
    issues: list[str] = []
    names = {Path(rel).name for rel in tree}
    for forbidden in sorted(names & FORBIDDEN_AUX_FILES):
        issues.append(f"{skill_name}: forbidden auxiliary file {forbidden}")
    if "SKILL.md" not in tree:
        issues.append(f"{skill_name}: SKILL.md is missing")
    for rel, data in tree.items():
        parts = Path(rel).parts
        if parts[0] == "scripts":
            if not rel.endswith(".py"):
                issues.append(f"{skill_name}: non-Python file under scripts/: {rel}")
            elif not data.startswith(b"#!/usr/bin/env python3\n"):
                issues.append(f"{skill_name}: {rel} lacks the python3 shebang")
    return issues


def _validate_executable_modes(skill_name: str, skill_dir: Path) -> list[str]:
    """Bundled files are invoked as ``python3 scripts/<name>.py`` — nothing in
    a shipped skill tree may carry the executable bit."""
    issues: list[str] = []
    for rel in _skill_files(skill_dir):
        if (skill_dir / rel).stat().st_mode & 0o111:
            issues.append(f"{skill_name}: {rel.as_posix()} carries the executable bit")
    return issues


def validate_skill(root: Path, skill_name: str) -> list[str]:
    canonical_dir = root / CANONICAL_SKILLS_DIR / skill_name
    if not canonical_dir.is_dir():
        return [f"{skill_name}: canonical source {canonical_dir} is missing"]
    tree = render_skill_tree(root, skill_name)
    skill_md = tree.get("SKILL.md", b"").decode("utf-8", errors="replace")
    issues = _validate_frontmatter(skill_name, skill_md)
    issues += _validate_references(skill_name, tree)
    issues += _validate_tree_shape(skill_name, tree)
    issues += _validate_executable_modes(skill_name, canonical_dir)
    codex_dir = root / CODEX_SKILLS_DIR / skill_name
    if codex_dir.is_dir():
        issues += _validate_executable_modes(skill_name, codex_dir)
    return issues


# ---------------------------------------------------------------------------
# Freshness: committed outputs must equal a fresh render, byte for byte
# ---------------------------------------------------------------------------


def _compare_tree(label: str, on_disk_dir: Path, rendered: dict[str, bytes]) -> list[str]:
    issues: list[str] = []
    on_disk = {
        rel.as_posix(): (on_disk_dir / rel).read_bytes()
        for rel in (_skill_files(on_disk_dir) if on_disk_dir.is_dir() else [])
    }
    for rel in sorted(set(rendered) - set(on_disk)):
        issues.append(f"{label}: missing generated file {rel}")
    for rel in sorted(set(on_disk) - set(rendered)):
        issues.append(f"{label}: extra file {rel} not produced by the materializer")
    for rel in sorted(set(rendered) & set(on_disk)):
        if rendered[rel] != on_disk[rel]:
            issues.append(f"{label}: {rel} is stale or manually edited")
    return issues


def check(root: Path) -> list[str]:
    issues: list[str] = []
    trees, provenance = render_all(root)
    for skill in SKILLS:
        issues += validate_skill(root, skill)
        issues += _compare_tree(
            f"canonical {CANONICAL_SKILLS_DIR / skill}",
            root / CANONICAL_SKILLS_DIR / skill,
            trees[skill],
        )
        issues += _compare_tree(
            f"generated {CODEX_SKILLS_DIR / skill}",
            root / CODEX_SKILLS_DIR / skill,
            trees[skill],
        )
    provenance_path = root / CODEX_SKILLS_DIR / PROVENANCE_NAME
    if not provenance_path.is_file():
        issues.append(f"generated {CODEX_SKILLS_DIR / PROVENANCE_NAME}: missing")
    elif provenance_path.read_bytes() != provenance:
        issues.append(
            f"generated {CODEX_SKILLS_DIR / PROVENANCE_NAME}: stale or manually edited"
        )
    return issues


def _write_tree(target_dir: Path, rendered: dict[str, bytes]) -> None:
    if target_dir.exists():
        shutil.rmtree(target_dir)
    for rel, data in rendered.items():
        path = target_dir / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def materialize(root: Path) -> dict:
    """Write every generated output and return the materialization report."""
    issues: list[str] = []
    for skill in SKILLS:
        issues += validate_skill(root, skill)
    if issues:
        return {"ok": False, "issues": issues, "written": []}
    trees, provenance = render_all(root)
    written: list[str] = []
    for skill in SKILLS:
        canonical_yaml = root / CANONICAL_SKILLS_DIR / skill / OPENAI_YAML
        if not canonical_yaml.exists() or canonical_yaml.read_bytes() != trees[skill][OPENAI_YAML.as_posix()]:
            canonical_yaml.parent.mkdir(parents=True, exist_ok=True)
            canonical_yaml.write_bytes(trees[skill][OPENAI_YAML.as_posix()])
            written.append((CANONICAL_SKILLS_DIR / skill / OPENAI_YAML).as_posix())
        _write_tree(root / CODEX_SKILLS_DIR / skill, trees[skill])
        written.append((CODEX_SKILLS_DIR / skill).as_posix())
    provenance_path = root / CODEX_SKILLS_DIR / PROVENANCE_NAME
    provenance_path.write_bytes(provenance)
    written.append((CODEX_SKILLS_DIR / PROVENANCE_NAME).as_posix())
    return {"ok": True, "issues": [], "written": written}


def build_report(root: Path, *, mode: str) -> dict:
    if mode == "check":
        issues = check(root)
        result = {"ok": not issues, "issues": issues, "written": []}
    else:
        result = materialize(root)
    return {
        "schema_version": SCHEMA_VERSION,
        "materializer_version": MATERIALIZER_VERSION,
        "mode": mode,
        "skills": list(SKILLS),
        "hosts": [dict(entry) for entry in HOST_MATRIX],
        **result,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Deterministic canonical-to-host materializer for the public skills."
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path(__file__).resolve().parents[1],
        help="scholialang-mcp checkout to materialize (default: this checkout)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify committed generated copies instead of writing them",
    )
    parser.add_argument("--json", action="store_true", help="emit the JSON report")
    args = parser.parse_args(argv)

    report = build_report(args.root.resolve(), mode="check" if args.check else "write")
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        for issue in report["issues"]:
            print(f"ISSUE: {issue}")
        for path in report["written"]:
            print(f"wrote {path}")
        print("ok" if report["ok"] else "FAILED")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
