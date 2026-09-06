"""Provenance, byte-parity, and shared conformance gates.

The vendored engine tracks the accepted scholialang 0.7.3 release (canonical
reference-target and collision-identity fix; ``fingerprint=`` support arrived
in 0.7.1). The shared action_recorded conformance corpus is
still cut at spec v0.6.2 — an additive validator revision must keep passing it
byte-for-byte, which is exactly what these gates assert.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
# Vendored validator engine: re-vendored from the accepted scholialang 0.7.3
# release commit (canonical reference-target and collision-identity fix).
VENDORED_VALIDATOR_VERSION = "0.7.3"
SCHOLIALANG_COMMIT = "9a86a4645c49074c4a415ade01093bff0e2ca70c"
# Shared action_recorded conformance corpus: unchanged, still cut at spec
# v0.6.2 (fingerprint fixtures live separately in scholialang-spec).
SPEC_CORPUS_VERSION = "0.6.2"
SPEC_COMMIT = "eb238069df1c907674f688a46fa23b8179263e1a"
HOSTS = ("claude-code", "codex", "ollama")
VENDOR_FILES = ("atoms.py", "parser.py", "validator.py", "serializer.py", "UPSTREAM.json", "LICENSE-MIT", "LICENSE-APACHE")


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _vendor_dir(host: str) -> Path:
    return (
        ROOT
        / "plugins"
        / host
        / "scholialang"
        / "scripts"
        / "_scholia_vendored"
    )


def _load_canonical_engine():
    package_name = "_scholia_v062_contract"
    package_dir = _vendor_dir("claude-code")
    spec = importlib.util.spec_from_file_location(
        package_name,
        package_dir / "__init__.py",
        submodule_search_locations=[str(package_dir)],
    )
    assert spec is not None and spec.loader is not None
    package = importlib.util.module_from_spec(spec)
    sys.modules[package_name] = package
    spec.loader.exec_module(package)
    return (
        importlib.import_module(f"{package_name}.parser"),
        importlib.import_module(f"{package_name}.validator"),
    )


class _FixtureGraph:
    def __init__(self, edges: list[dict[str, str]]):
        self.edges = edges

    def has_edge(
        self,
        *,
        edge_type: str,
        source_id: str | None = None,
        target_id: str | None = None,
    ) -> bool:
        return any(
            edge["relation"] == edge_type
            and (source_id is None or edge["source_id"] == source_id)
            and (target_id is None or edge["target_id"] == target_id)
            for edge in self.edges
        )


def test_vendored_engine_provenance_and_byte_identity():
    canonical_dir = _vendor_dir("claude-code")
    provenance = json.loads((canonical_dir / "UPSTREAM.json").read_text(encoding="utf-8"))
    assert provenance["source"] == "https://github.com/dougfirlabs/scholialang"
    assert provenance["commit"] == SCHOLIALANG_COMMIT
    assert provenance["validator_version"] == VENDORED_VALIDATOR_VERSION

    for name in VENDOR_FILES:
        canonical = (canonical_dir / name).read_bytes()
        for host in HOSTS[1:]:
            assert (_vendor_dir(host) / name).read_bytes() == canonical
        if name.endswith(".py") and name != "__init__.py":
            assert _sha256(canonical) == provenance["files"][name]["vendored_sha256"]


def test_shared_spec_corpus_provenance():
    fixture_dir = ROOT / "tests" / "fixtures" / "scholialang-spec" / "v0.6.2"
    provenance = json.loads((fixture_dir / "UPSTREAM.json").read_text(encoding="utf-8"))
    assert provenance["source"] == "https://github.com/dougfirlabs/scholialang-spec"
    assert provenance["commit"] == SPEC_COMMIT
    assert provenance["spec_version"] == SPEC_CORPUS_VERSION
    for name, metadata in provenance["files"].items():
        assert _sha256((fixture_dir / name).read_bytes()) == metadata["sha256"]


def test_vendored_engine_passes_all_shared_action_recorded_cases():
    fixture_path = (
        ROOT
        / "tests"
        / "fixtures"
        / "scholialang-spec"
        / "v0.6.2"
        / "action_recorded.json"
    )
    corpus = json.loads(fixture_path.read_text(encoding="utf-8"))
    assert len(corpus["cases"]) == 13
    parser, validator = _load_canonical_engine()
    assert validator.SCHOLIA_VALIDATOR_VERSION == VENDORED_VALIDATOR_VERSION

    for case in corpus["cases"]:
        trace = parser.parse(case["trace"])
        graph = _FixtureGraph(case["graph_edges"])
        result = validator.validate(trace, graph=graph)
        errors = result.errors_by_rule["action_recorded"]
        expected = case["expects"]
        assert len(errors) == expected["error_count"], case["id"]
        assert (not errors) == (expected["outcome"] == "pass"), case["id"]
        if "atom_ids" in expected:
            assert [error.atom_id for error in errors] == expected["atom_ids"], case["id"]


def test_vendored_engine_recognizes_fingerprint_attribute():
    """0.7.x parity (additive since 0.7.1, preserved by the 0.7.3
    re-vendor): the offline vendored validator recognizes the additive
    ``fingerprint=`` attribute on ``<Observation>`` and enforces the
    ``fingerprint_well_formed`` rule (FINGERPRINT.md §3) without any network or
    PyPI install. This is the offline parity gate for the re-vendored snapshot.
    """
    parser, validator = _load_canonical_engine()
    graph = _FixtureGraph([])

    def _fingerprint_errors(trace_xml: str):
        result = validator.validate(parser.parse(trace_xml), graph=graph)
        return result.errors_by_rule["fingerprint_well_formed"]

    # Well-formed ``<algo>:<hex>`` fingerprint bound to a location → recognized
    # and accepted (the attribute is known; no hard-fail).
    assert _fingerprint_errors(
        '<Step id="S_01">'
        '<Observation id="Obs_01" location="src/a.py:1:2" '
        'fingerprint="sha256:deadbeef">saw</Observation>'
        "</Step>"
    ) == []

    # A malformed value (no ``algo:`` prefix / non-hex) is a hard-fail.
    malformed = _fingerprint_errors(
        '<Step id="S_01">'
        '<Observation id="Obs_01" location="src/a.py:1:2" '
        'fingerprint="NOTHEX">saw</Observation>'
        "</Step>"
    )
    assert [error.atom_id for error in malformed] == ["Obs_01"]

    # A fingerprint with no ``location`` to bind is a hard-fail.
    unbound = _fingerprint_errors(
        '<Step id="S_01">'
        '<Observation id="Obs_01" fingerprint="sha256:deadbeef">saw</Observation>'
        "</Step>"
    )
    assert [error.atom_id for error in unbound] == ["Obs_01"]

    # Ignore-if-absent: a fingerprint-less trace is vacuously clean.
    assert _fingerprint_errors(
        '<Step id="S_01">'
        '<Observation id="Obs_01" location="src/a.py:1:2">saw</Observation>'
        "</Step>"
    ) == []
