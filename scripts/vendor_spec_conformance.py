#!/usr/bin/env python3
"""Vendor the shared Scholia v0.6.2 conformance contract from an exact commit."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path


SPEC_VERSION = "0.6.2"
SOURCE_FILES = (
    "conformance/v0.6.2/README.md",
    "conformance/v0.6.2/action_recorded.json",
)


def _git(repo: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        stdout=subprocess.PIPE,
    ).stdout


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("source_repo", type=Path)
    parser.add_argument("commit", help="exact scholialang-spec commit or tag")
    args = parser.parse_args()

    source_repo = args.source_repo.resolve()
    if not (source_repo / ".git").exists():
        raise SystemExit(f"not a Git checkout: {source_repo}")
    commit = _git(
        source_repo,
        "rev-parse",
        "--verify",
        f"{args.commit}^{{commit}}",
    ).decode().strip()

    blobs = {
        source_path: _git(source_repo, "show", f"{commit}:{source_path}")
        for source_path in SOURCE_FILES
    }
    corpus = json.loads(blobs["conformance/v0.6.2/action_recorded.json"])
    cases = corpus.get("cases", [])
    categories = {case.get("category") for case in cases}
    if (
        corpus.get("spec_version") != SPEC_VERSION
        or corpus.get("validator_version") != SPEC_VERSION
        or corpus.get("rule") != "action_recorded"
        or len(cases) != 13
        or categories != {"positive", "negative"}
    ):
        raise SystemExit("unexpected v0.6.2 action_recorded conformance corpus")

    destination = (
        Path(__file__).resolve().parents[1]
        / "tests"
        / "fixtures"
        / "scholialang-spec"
        / "v0.6.2"
    )
    destination.mkdir(parents=True, exist_ok=True)
    for source_path, payload in blobs.items():
        (destination / Path(source_path).name).write_bytes(payload)

    provenance = {
        "schema": 1,
        "source": "https://github.com/dougfirlabs/scholialang-spec",
        "commit": commit,
        "spec_version": SPEC_VERSION,
        "files": {
            Path(source_path).name: {
                "source_path": source_path,
                "sha256": _sha256(payload),
            }
            for source_path, payload in blobs.items()
        },
    }
    (destination / "UPSTREAM.json").write_text(
        json.dumps(provenance, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"vendored {len(cases)} Scholia {SPEC_VERSION} cases from {commit}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
