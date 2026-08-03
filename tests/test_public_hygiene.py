"""Public-package hygiene guard — fails on internal references.

scholialang-mcp ships publicly (PyPI + the three plugin marketplaces), so no
internal-only token may leak into the published surfaces. The historical leak
vector was the vendored `_scholia_vendored/` snapshots, which once carried an
internal codename and a strategic-timing comment; the v0.6.1 re-sync scrubbed
them, and this guard keeps them scrubbed.

Two tests:

* ``test_published_surfaces_have_no_internal_references`` scans the repo and
  hard-fails if any forbidden token appears.
* ``test_guard_catches_a_planted_internal_reference`` is the self-test: it
  plants a known internal token and asserts the scanner catches it, so a
  defanged regex can't pass silently.

Mirrors the leak-guard shape used in the scholialang-spec repo. The CI mirror
lives in ``.github/workflows/hygiene.yml`` — keep the forbidden set in lockstep
with the ``grep`` pattern there.
"""
from __future__ import annotations

import re
from pathlib import Path

# Internal-only tokens that must never ship publicly: internal codenames,
# internal infra markers, and strategic-timing phrasing. Public identity
# (Doug Fir Labs, dougfirlabs, scholialang, the LICENSE/NOTICE) is deliberately
# NOT here. Compiled case-insensitively (see ``_PATTERN``).
FORBIDDEN_REGEX = r"opentalon|\bT42\b|\bT6x7\b|proofdag|MS-?Co-?Pilot"

_PATTERN = re.compile(FORBIDDEN_REGEX, re.IGNORECASE)

_THIS_FILE = Path(__file__).resolve()


def _repo_root() -> Path:
    return _THIS_FILE.parents[1]


# Directory names pruned from the walk: VCS, build/cache artifacts, and the
# uncommitted RSI/operator scratch dirs. ``.github`` is pruned because the CI
# mirror necessarily spells out the forbidden tokens in its grep pattern.
_PRUNE_DIRS = {
    ".git",
    ".github",
    ".ralph",
    ".opentalon",
    ".scholia",
    ".scholialang",
    ".pytest_cache",
    "__pycache__",
    ".venv",
    "dist",
    "build",
    "node_modules",
}

# Trace-store dirs that are pruned from the walk must ALSO be gitignored.
# Pruning tells the scanner to look away; gitignoring is what makes that safe
# in a PUBLIC repo. Pruning without ignoring is the dangerous combination -- a
# `git add -A` could then commit local trace data into a directory the guard
# never reads.
#
# The two internal RSI/operator scratch dirs are intentionally absent from this
# set: naming an internal codename in a public .gitignore would itself trip
# (and should trip) the forbidden-token scan. They are ignored locally via
# .git/info/exclude, which is never published.
_MUST_BE_GITIGNORED = {".scholia", ".scholialang"}

_CI_WORKFLOW = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "hygiene.yml"


def _iter_scan_files():
    root = _repo_root()
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        if path.resolve() == _THIS_FILE:
            # The guard itself necessarily names the forbidden tokens.
            continue
        if _PRUNE_DIRS & set(path.relative_to(root).parts):
            continue
        yield path


def _text_or_none(path: Path):
    try:
        return path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError):
        return None


def test_published_surfaces_have_no_internal_references():
    offenders = []
    root = _repo_root()
    for path in _iter_scan_files():
        text = _text_or_none(path)
        if text is None:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _PATTERN.search(line):
                offenders.append(f"{path.relative_to(root)}:{lineno}: {line.strip()}")
    assert not offenders, "Internal references found in published surfaces:\n" + "\n".join(offenders)


def test_guard_catches_a_planted_internal_reference(tmp_path):
    planted = tmp_path / "planted.txt"
    planted.write_text("this mentions opentalon internally\n", encoding="utf-8")
    assert _PATTERN.search(planted.read_text(encoding="utf-8")), (
        "guard self-test FAILED — the forbidden regex did not catch a planted internal reference."
    )


def test_pruned_scratch_dirs_are_gitignored():
    """Pruning a dir from the scan is only safe if it cannot be committed.

    #37 added ``.scholia`` to the prune set while it was not in ``.gitignore``,
    which is the one combination that actually loses coverage on a public repo.
    This asserts the two stay paired.
    """
    ignored = (_repo_root() / ".gitignore").read_text(encoding="utf-8")
    entries = {line.strip().rstrip("/") for line in ignored.splitlines() if line.strip()}
    missing = sorted(d for d in _MUST_BE_GITIGNORED if d not in entries)
    assert not missing, (
        f"pruned from the leak scan but not gitignored: {missing}. Either add them "
        "to .gitignore or stop pruning them — pruning an unignored dir in a public "
        "repo is a blind spot."
    )


def test_ci_mirror_is_in_lockstep_with_this_guard():
    """The module docstring asks a human to keep CI in lockstep. Enforce it.

    The CI grep in hygiene.yml is the authoritative gate, but this Python guard
    is what runs locally. When they diverge, one of them is silently weaker —
    exactly what happened when ``.scholia`` was pruned here and not there.
    """
    workflow = _CI_WORKFLOW.read_text(encoding="utf-8")

    assert FORBIDDEN_REGEX in workflow, (
        "hygiene.yml no longer greps FORBIDDEN_REGEX verbatim; the CI mirror and "
        "this guard have drifted apart."
    )

    ci_excluded = set(re.findall(r"--exclude-dir=([^\s\\]+)", workflow))
    # Every dir this guard skips must also be skipped by CI, or CI reports
    # failures on paths the local run never reads.
    only_local = sorted(d for d in _PRUNE_DIRS if d not in ci_excluded and d != ".venv")
    assert not only_local, (
        f"pruned locally but not excluded in hygiene.yml: {only_local}. Add matching "
        "--exclude-dir entries so local and CI agree on what is scanned."
    )
