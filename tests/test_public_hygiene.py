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
    ".pytest_cache",
    "__pycache__",
    ".venv",
    "dist",
    "build",
    "node_modules",
}


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
