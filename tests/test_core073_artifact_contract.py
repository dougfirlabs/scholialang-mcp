"""Accepted artifact identity, installed origins, and exact semantic corpus.

Required inputs are checked in and never skipped. Each host runs the accepted
core's unmodified conformance runner in a child with only that vendor package
on its import path, so an installed core cannot hide an incomplete bundle.
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from scripts.verify_core_input import verify


ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/scholialang-spec/v0.7.0"
HOSTS = ("claude-code", "codex", "ollama")


def test_accepted_artifact_and_license_identity():
    receipt = verify(ROOT)
    assert receipt["commit"] == "9a86a4645c49074c4a415ade01093bff0e2ca70c"
    assert receipt["validator_version"] == "0.7.3"
    assert receipt["grammar_version"] == "0.7.0"
    assert receipt["artifacts"]["scholialang-0.7.3-py3-none-any.whl"] == (
        "76c9e9a15cb3039bcf59a63b4727271cc9933745eecd4280712c5df65616317a"
    )
    for host in HOSTS:
        vendor = ROOT / "plugins" / host / "scholialang/scripts/_scholia_vendored"
        upstream = json.loads((vendor / "UPSTREAM.json").read_text())
        assert upstream["commit"] == receipt["commit"]
        for name, metadata in upstream["files"].items():
            assert hashlib.sha256((vendor / name).read_bytes()).hexdigest() == metadata["vendored_sha256"]
        assert "serializer.py" in upstream["files"]


def test_corrupted_artifact_is_rejected(tmp_path):
    shutil.copytree(ROOT / "vendor", tmp_path / "vendor")
    wheel = tmp_path / "vendor/core/scholialang-0.7.3-py3-none-any.whl"
    wheel.write_bytes(wheel.read_bytes() + b"tampered")
    with pytest.raises(ValueError, match="artifact hash mismatch"):
        verify(tmp_path)


def test_semantic_corpus_identity():
    upstream = json.loads((FIXTURES / "UPSTREAM.json").read_text())
    assert upstream["spec_commit"] == (ROOT / "scholialang-spec-ref.txt").read_text().strip()
    for name, digest in upstream["files"].items():
        assert hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest() == digest
    assert upstream["files"]["semantic_atoms.json"] == (
        "8ef6db811290522e6234fbc9d1b748d567aaa3b78f86c536527c98e77a8f3a6e"
    )


@pytest.mark.parametrize("host", ("installed", *HOSTS))
def test_exact_semantic_corpus_and_roundtrips(host, tmp_path):
    if host != "installed":
        shutil.copytree(
            ROOT / "plugins" / host / "scholialang/scripts/_scholia_vendored",
            tmp_path / "scholialang",
        )
    code = """
import json, runpy, sys
from pathlib import Path
host, isolated, fixtures = sys.argv[1:]
if host != 'installed':
    sys.path.insert(0, isolated)
from scholialang import atoms, parser, serializer, validator
assert atoms.SCHOLIA_VALIDATOR_VERSION == '0.7.3'
assert atoms.SCHOLIA_GRAMMAR_VERSION == '0.7.0'
assert len(atoms.ATOM_KINDS) == 35
if host != 'installed':
    for module in (atoms, parser, serializer, validator):
        assert Path(module.__file__).is_relative_to(isolated)
runner = runpy.run_path(str(Path(fixtures) / 'run_spec_conformance.py'))
count, failures = runner['run_semantic_suite'](
    Path(fixtures) / 'semantic_atoms.json', Path(fixtures) / 'coverage-inventory.json')
assert count == 204, count
assert not failures, failures
print(json.dumps({'host': host, 'cases': count, 'origin': atoms.__file__}))
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", code, host, str(tmp_path), str(FIXTURES)],
        cwd=tmp_path, text=True, capture_output=True, timeout=10,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["cases"] == 204


def test_installed_core_serializer_and_yaml_closure():
    import scholialang
    import scholialang.serializer
    import scholialang_mcp
    import yaml

    assert importlib.metadata.version("scholialang") == scholialang.__version__ == "0.7.3"
    assert importlib.metadata.version("scholialang-mcp") == scholialang_mcp.__version__ == "0.7.3"
    assert importlib.metadata.version("PyYAML") == yaml.__version__
    with zipfile.ZipFile(ROOT / "vendor/core/scholialang-0.7.3-py3-none-any.whl") as wheel:
        package = Path(scholialang.__file__).parent
        for name in wheel.namelist():
            if name.startswith("scholialang/") and name.endswith(".py"):
                assert (package / name.removeprefix("scholialang/")).read_bytes() == wheel.read(name), name
    prefix = os.environ.get("MCP_EXPECT_IMPORT_PREFIX")
    if prefix:
        for module in (scholialang, scholialang.serializer, scholialang_mcp, yaml):
            assert Path(module.__file__).resolve().is_relative_to(Path(prefix).resolve()), module.__file__
