"""Shared fingerprint= fixture consumption against the VENDORED engine.

#37 states that the fingerprint contract this plugin validates against matches
scholialang-spec ``9c1fcfa`` and is "referenced, not forked". Nothing in the
repo referenced that commit, and the offline gate in
``test_v062_vendor_contract.py`` hand-writes its own inline traces. Those
inline cases are good tests, but they are a *reimplementation* of the contract:
if the spec's fixtures or its well-formedness regex changed, they would keep
passing and the claimed parity would be silently false.

This module closes that loop the way the scholialang reference suite already
does -- by consuming the single shared corpus rather than a fork:

* ``scholialang-spec-ref.txt`` pins the exact merged contract commit.
* CI (``.github/workflows/spec-parity.yml``) clones the spec at that SHA and
  sets ``MCP_REQUIRE_FINGERPRINT_FIXTURES=1`` so a missing or truncated corpus
  is RED, never a green skip.
* Locally, and in the ordinary offline ``test`` job, the module SKIPS when no
  spec checkout is present -- the corpus is shared, not vendored, so absence is
  a missing dependency, never a copied-in fallback.

Only the **notation** layer is asserted here (spec 3): the
``fingerprint_well_formed`` rule, decided on the trace alone. The
verifies / rebinds / span_mismatch / stale verdicts are **consumer** layer
(spec 5) -- they recompute a digest over source using 52X-B2's single
definition, which a notation validator has no access to. For those fixtures we
assert only the notation outcome the manifest declares, and record the consumer
verdict without executing it.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import sys
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")


ROOT = Path(__file__).resolve().parents[1]
RULE = "fingerprint_well_formed"

_SPEC_ENV = "SCHOLIALANG_SPEC_DIR"
# Set by spec-parity.yml, which checks the spec out at the pinned SHA. When
# "1", a missing corpus is a hard failure rather than a skip -- a parity gate
# must prove the fixtures ran, never report green while validating zero.
_REQUIRE_ENV = "MCP_REQUIRE_FINGERPRINT_FIXTURES"
_REQUIRED_FIXTURES = frozenset({
    "valid_fingerprint", "moved_symbol_rebind", "ignore_if_absent",
    "malformed_hash", "span_mismatch", "stale_fingerprint",
})
_FIXTURE_SUBPATH = Path("tests") / "fixtures" / "fingerprint"


def pinned_spec_ref() -> str:
    return (ROOT / "scholialang-spec-ref.txt").read_text(encoding="utf-8").strip()


def _candidate_spec_dirs() -> list[Path]:
    candidates: list[Path] = []
    env = os.environ.get(_SPEC_ENV)
    if env:
        candidates.append(Path(env))
    candidates.append(ROOT.parent / "scholialang-spec")
    candidates.append(ROOT.parent.parent / "scholialang-spec")
    return candidates


def _find_fixture_dir() -> Path | None:
    for spec_dir in _candidate_spec_dirs():
        fixtures = spec_dir / _FIXTURE_SUBPATH
        if (fixtures / "manifest.yaml").is_file():
            return fixtures
    return None


_FIXTURE_DIR = _find_fixture_dir()
_REQUIRE_FIXTURES = os.environ.get(_REQUIRE_ENV) == "1"

pytestmark = pytest.mark.skipif(
    _FIXTURE_DIR is None and not _REQUIRE_FIXTURES,
    reason=(
        f"scholialang-spec fingerprint fixtures not found; set {_SPEC_ENV} or "
        "place a scholialang-spec checkout beside this repo (shared corpus, "
        "no in-repo fork)."
    ),
)


def _load_vendored_engine():
    """Load the shipped offline snapshot, not an installed scholialang.

    Deliberately distinct from the package name used by
    ``test_v062_vendor_contract.py`` so the two modules cannot clobber each
    other in ``sys.modules`` regardless of collection order.
    """
    package_name = "_scholia_fingerprint_parity"
    package_dir = ROOT / "plugins" / "claude-code" / "scholialang" / "scripts" / "_scholia_vendored"
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


def _manifest() -> dict:
    assert _FIXTURE_DIR is not None
    return yaml.safe_load((_FIXTURE_DIR / "manifest.yaml").read_text(encoding="utf-8"))


def _fingerprint_errors(trace_xml: str):
    parser, validator = _load_vendored_engine()
    result = validator.validate(parser.parse(trace_xml))
    return result.errors_by_rule[RULE]


def test_fingerprint_corpus_present_when_required() -> None:
    """Fail closed for the dedicated parity gate."""
    if not _REQUIRE_FIXTURES:
        pytest.skip(f"{_REQUIRE_ENV} not set (offline checkout)")
    assert _FIXTURE_DIR is not None, (
        f"{_REQUIRE_ENV}=1 but no scholialang-spec fingerprint corpus resolved. "
        f"Expected a checkout at the pinned ref {pinned_spec_ref()}."
    )
    names = {entry["name"] for entry in _manifest()["fixtures"]}
    missing = _REQUIRED_FIXTURES - names
    assert not missing, f"shared fingerprint corpus is truncated; missing: {sorted(missing)}"


def test_vendored_engine_matches_every_declared_notation_verdict() -> None:
    """The offline engine agrees with the shared corpus on all six fixtures.

    This is the assertion that makes the "referenced, not forked" claim real:
    the expected outcomes come from the spec's own manifest, not from values
    retyped into this repo.
    """
    manifest = _manifest()
    assert _FIXTURE_DIR is not None

    checked = 0
    for entry in manifest["fixtures"]:
        trace_xml = (_FIXTURE_DIR / entry["trace"]).read_text(encoding="utf-8")
        errors = _fingerprint_errors(trace_xml)
        declared_valid = entry["notation_valid"]
        assert bool(errors) != bool(declared_valid), (
            f"{entry['name']}: manifest declares notation_valid={declared_valid} "
            f"but the vendored engine produced {RULE} errors={errors!r}"
        )
        checked += 1

    assert checked == len(_REQUIRED_FIXTURES), (
        f"expected {len(_REQUIRED_FIXTURES)} fixtures, drove {checked}"
    )


def test_consumer_layer_fixtures_are_notation_clean() -> None:
    """span_mismatch and stale are consumer failures, not notation failures.

    A notation validator must NOT flag them (spec 3 vs 5). If the vendored
    engine ever started recomputing digests it would fail this test, which is
    the intended tripwire.
    """
    consumer = [e for e in _manifest()["fixtures"] if e["layer"] == "consumer"]
    assert consumer, "manifest declares no consumer-layer fixtures"
    assert _FIXTURE_DIR is not None
    for entry in consumer:
        trace_xml = (_FIXTURE_DIR / entry["trace"]).read_text(encoding="utf-8")
        assert _fingerprint_errors(trace_xml) == [], (
            f"{entry['name']} (declared verdict {entry['expected']!r}) is a "
            "consumer-layer case; the notation validator must not flag it"
        )


def test_vendored_rule_agrees_with_the_manifest_regex() -> None:
    """Behavioral parity with the contract's declared well-formedness regex.

    The manifest publishes ``well_formed_regex``. Rather than trusting that the
    vendored implementation still matches it, drive representative values
    through both and require the same verdict -- so a regex change in the spec
    surfaces here instead of passing silently.
    """
    import re

    pattern = re.compile(_manifest()["well_formed_regex"])
    probes = [
        "sha256:8f4a9d2c1b3e",
        "sha256:deadbeef",
        "sha1:abc123",
        "sha256:NOTHEX!!",
        "sha256:DEADBEEF",
        "deadbeef",
        "sha256:",
        ":deadbeef",
    ]
    for value in probes:
        trace = (
            '<Step id="s1">'
            f'<Observation id="Obs_01" location="src/a.py:1:2" fingerprint="{value}">x</Observation>'
            "</Step>"
        )
        spec_says_valid = pattern.match(value) is not None
        engine_says_valid = not _fingerprint_errors(trace)
        assert engine_says_valid == spec_says_valid, (
            f"fingerprint {value!r}: spec regex says valid={spec_says_valid}, "
            f"vendored engine says valid={engine_says_valid}"
        )


def test_fingerprint_requires_a_location_to_bind() -> None:
    """Spec 3 clause 3: a fingerprint with no location is a hard fail."""
    trace = (
        '<Step id="s1">'
        '<Observation id="Obs_01" fingerprint="sha256:deadbeef">x</Observation>'
        "</Step>"
    )
    assert [error.atom_id for error in _fingerprint_errors(trace)] == ["Obs_01"]
