#!/usr/bin/env python3
"""Vendor a deterministic Scholialang snapshot into the canonical plugin.

The source is read from an exact Git commit, not from the source checkout's
working tree. After this command succeeds, run ``scripts/sync_plugins.sh`` to
propagate the canonical snapshot to the Codex and Ollama plugins.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path


RELEASE_VERSION = "0.7.3"
SOURCE_FILES = {
    "atoms.py": "src/scholialang/atoms.py",
    "parser.py": "src/scholialang/parser.py",
    "validator.py": "src/scholialang/validator.py",
}
IMPORT_REWRITES = {
    "parser.py": (
        ("from scholialang.atoms import (", "from .atoms import ("),
        ("from scholialang import atoms as _atoms_module", "from . import atoms as _atoms_module"),
    ),
    "validator.py": (
        ("from scholialang.atoms import (", "from .atoms import ("),
    ),
}


def _git(repo: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        stdout=subprocess.PIPE,
    ).stdout


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _rewrite_imports(name: str, source: bytes) -> bytes:
    text = source.decode("utf-8")
    for old, new in IMPORT_REWRITES.get(name, ()):
        count = text.count(old)
        if count != 1:
            raise SystemExit(
                f"{name}: expected exactly one {old!r} import, found {count}; "
                "review the upstream import surface before vendoring"
            )
        text = text.replace(old, new)
    if re.search(r"^(?:from|import) scholialang(?:\.|\s)", text, re.MULTILINE):
        raise SystemExit(f"{name}: unsupported absolute Scholialang import remains after rewrite")
    return text.encode("utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source_repo", type=Path)
    parser.add_argument("commit", help="exact Scholialang commit or ref to vendor")
    parser.add_argument("--expected-version", default=RELEASE_VERSION)
    args = parser.parse_args()

    source_repo = args.source_repo.resolve()
    if not (source_repo / ".git").exists():
        raise SystemExit(f"not a Git checkout: {source_repo}")

    commit = _git(source_repo, "rev-parse", "--verify", f"{args.commit}^{{commit}}").decode().strip()
    repo_root = Path(__file__).resolve().parents[1]
    destination = (
        repo_root
        / "plugins"
        / "claude-code"
        / "scholialang"
        / "scripts"
        / "_scholia_vendored"
    )

    rendered: dict[str, bytes] = {}
    provenance_files: dict[str, dict[str, str]] = {}
    for name, source_path in SOURCE_FILES.items():
        source = _git(source_repo, "show", f"{commit}:{source_path}")
        vendored = _rewrite_imports(name, source)
        compile(vendored, f"{commit}:{source_path}", "exec")
        rendered[name] = vendored
        provenance_files[name] = {
            "source_path": source_path,
            "source_sha256": _sha256(source),
            "vendored_sha256": _sha256(vendored),
        }

    atoms = rendered["atoms.py"].decode("utf-8")
    marker = re.search(
        r'^SCHOLIA_VALIDATOR_VERSION:\s*str\s*=\s*"([^"]+)"',
        atoms,
        re.MULTILINE,
    )
    actual_version = marker.group(1) if marker else None
    if actual_version != args.expected_version:
        raise SystemExit(
            "upstream validator version mismatch: "
            f"expected {args.expected_version!r}, found {actual_version!r}"
        )

    destination.mkdir(parents=True, exist_ok=True)
    for name, payload in rendered.items():
        (destination / name).write_bytes(payload)

    provenance = {
        "schema": 1,
        "source": "https://github.com/dougfirlabs/scholialang",
        "commit": commit,
        "validator_version": actual_version,
        "files": provenance_files,
        "import_rewrites": {
            name: [{"from": old, "to": new} for old, new in rewrites]
            for name, rewrites in IMPORT_REWRITES.items()
        },
    }
    (destination / "UPSTREAM.json").write_text(
        json.dumps(provenance, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"vendored Scholialang {actual_version} from {commit}")
    print("next: scripts/sync_plugins.sh")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
