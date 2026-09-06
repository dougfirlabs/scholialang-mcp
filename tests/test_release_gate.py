"""Contract tests for the no-publish release gate.

Covers the PRD gates: the report records the source commit, every version
axis, materialization freshness, and required-suite verdicts; artifacts are
built from clean isolated checkouts with reproducible hashes; the version
recommendation is non-binding and never applied; every publication gate is
recorded as ``performed: false`` and the gate has zero publication side
effects; skipped required work is ``incomplete``, never a silent pass.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
GATE_SCRIPT = ROOT / "scripts" / "release_gate.py"

_SPEC = importlib.util.spec_from_file_location("release_gate", GATE_SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
gate = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(gate)

RELEASE = "0.7.3"
GRAMMAR = "0.7.0"


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=True, timeout=60
    ).stdout.strip()


def _fast_report(output_dir: Path, **kwargs) -> dict:
    kwargs.setdefault("suites", ())
    kwargs.setdefault("build_artifacts", False)
    return gate.build_gate_report(ROOT, output_dir, **kwargs)


def _passing_suite() -> list[dict]:
    return [{"id": "fake_pass", "command": [sys.executable, "-c", "pass"], "required": True}]


# ---------------------------------------------------------------------------
# Evidence contents
# ---------------------------------------------------------------------------


@pytest.mark.timeout(120)
def test_report_records_source_commit_and_aligned_version_axes(tmp_path):
    report = _fast_report(tmp_path / "out")

    assert report["source"]["commit"] == _git("rev-parse", "HEAD")
    assert report["source"]["branch"] == _git("rev-parse", "--abbrev-ref", "HEAD")

    axes = report["version_axes"]
    assert axes["package"] == RELEASE
    assert axes["grammar"] == GRAMMAR
    assert axes["grammar_corpus_present"] is True
    assert axes["marketplace"] == RELEASE
    assert set(axes["plugins"].values()) == {RELEASE}
    assert set(axes["vendored_validator"].values()) == {RELEASE}
    for skill in ("scholialang-doctor", "scholialang-verify"):
        assert axes["skills"][skill] == {"version": RELEASE, "grammar": GRAMMAR}
    assert axes["aligned"] is True


@pytest.mark.timeout(120)
def test_materialization_and_host_honesty_are_recorded(tmp_path):
    report = _fast_report(tmp_path / "out")
    materialization = report["materialization"]
    assert materialization["fresh"] is True
    assert materialization["deterministic"] is True
    assert materialization["issues"] == []
    by_host = {entry["host"]: entry for entry in materialization["hosts"]}
    assert by_host["claude-desktop"]["native_skill_surface"] is False
    assert by_host["claude-code"]["native_skill_surface"] is True


@pytest.mark.timeout(120)
def test_version_recommendation_is_non_binding_and_never_applied(tmp_path):
    pyproject_before = (ROOT / "pyproject.toml").read_bytes()
    report = _fast_report(tmp_path / "out")
    recommendation = report["version_recommendation"]

    assert recommendation["binding"] is False
    assert recommendation["applied"] is False
    assert recommendation["current_version"] == RELEASE
    assert recommendation["rationale"]
    if recommendation["released_tag"] is None:
        assert recommendation["action"] == "release_current_version"
    elif recommendation["commits_since_released_tag"] == 0:
        assert recommendation["action"] == "none"
    else:
        # A released v0.7.3 tag with newer commits: a bump is required but the
        # gate must not choose it.
        assert recommendation["action"] == "bump_before_publish"
        assert recommendation["mechanically_unambiguous"] is False
        assert "no version is chosen" in recommendation["rationale"]
    assert (ROOT / "pyproject.toml").read_bytes() == pyproject_before


@pytest.mark.timeout(120)
def test_every_publication_gate_is_recorded_unperformed_with_no_side_effects(tmp_path):
    tags_before = _git("tag", "--points-at", "HEAD")
    status_before = _git("status", "--porcelain")

    report = _fast_report(tmp_path / "out")

    assert {entry["gate"] for entry in report["publication"]} == set(gate.PUBLICATION_GATES)
    assert all(entry["performed"] is False for entry in report["publication"])
    assert all(entry["blocked_on"] for entry in report["publication"])
    assert report["policy"]["no_publish"] is True
    assert _git("tag", "--points-at", "HEAD") == tags_before
    assert _git("status", "--porcelain") == status_before


@pytest.mark.timeout(120)
def test_report_is_machine_readable_and_deterministic(tmp_path):
    payloads = []
    for run in ("first", "second"):
        _fast_report(tmp_path / run)
        payloads.append((tmp_path / run / "release_gate.json").read_bytes())
    assert payloads[0] == payloads[1]
    parsed = json.loads(payloads[0])
    assert parsed["schema_version"] == gate.SCHEMA_VERSION
    assert parsed["residual_risks"]


# ---------------------------------------------------------------------------
# Verdict derivation: skips are incomplete, failures fail, nothing silent
# ---------------------------------------------------------------------------


@pytest.mark.timeout(120)
def test_skipped_suites_and_artifacts_are_incomplete_never_pass(tmp_path):
    report = _fast_report(tmp_path / "out")
    assert report["overall"]["verdict"] == "incomplete"
    reasons = " | ".join(report["overall"]["reasons"])
    assert "suites were skipped" in reasons
    assert "artifact builds were skipped" in reasons


@pytest.mark.timeout(120)
def test_required_suite_failure_fails_the_gate(tmp_path):
    failing = [
        {
            "id": "fake_fail",
            "command": [sys.executable, "-c", "import sys; sys.exit(1)"],
            "required": True,
        }
    ]
    report = _fast_report(tmp_path / "out", suites=failing)
    suite = report["tests"]["suites"][0]
    assert suite["status"] == "fail"
    assert suite["returncode"] == 1
    assert report["overall"]["verdict"] == "fail"
    assert "required suite fake_fail did not pass" in report["overall"]["reasons"]


@pytest.mark.timeout(120)
def test_optional_suite_failure_does_not_flip_the_verdict(tmp_path):
    optional = [
        *_passing_suite(),
        {
            "id": "fake_optional",
            "command": [sys.executable, "-c", "import sys; sys.exit(1)"],
            "required": False,
        },
    ]
    report = _fast_report(tmp_path / "out", suites=optional)
    assert report["overall"]["verdict"] == "incomplete"  # artifacts still skipped
    assert "fake_optional" not in " | ".join(report["overall"]["reasons"])


@pytest.mark.timeout(120)
def test_cli_exit_code_tracks_the_verdict(tmp_path):
    code = gate.main(
        [
            "--root",
            str(ROOT),
            "--output-dir",
            str(tmp_path / "out"),
            "--skip-suites",
            "--skip-artifacts",
        ]
    )
    assert code == 1
    report = json.loads((tmp_path / "out" / "release_gate.json").read_text(encoding="utf-8"))
    assert report["overall"]["verdict"] == "incomplete"


@pytest.mark.timeout(120)
def test_cli_exit_code_two_on_required_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(
        gate,
        "REQUIRED_SUITES",
        [
            {
                "id": "fake_fail",
                "command": [sys.executable, "-c", "import sys; sys.exit(1)"],
                "required": True,
            }
        ],
    )
    code = gate.main(
        ["--root", str(ROOT), "--output-dir", str(tmp_path / "out"), "--skip-artifacts"]
    )
    assert code == 2


# ---------------------------------------------------------------------------
# Clean-checkout artifact builds (heavy: two pip wheel runs + mcpb staging)
# ---------------------------------------------------------------------------


@pytest.mark.timeout(600)
def test_clean_checkout_artifacts_are_reproducible_hashed_and_honest(tmp_path):
    report = gate.build_gate_report(
        ROOT,
        tmp_path / "out",
        suites=_passing_suite(),
        build_artifacts=True,
    )

    wheel = report["artifacts"]["wheel"]
    assert wheel["reproducible"] is True, wheel["builds"]
    hashes = {build["sha256"] for build in wheel["builds"]}
    assert len(hashes) == 1 and None not in hashes
    artifact = tmp_path / "out" / wheel["artifact"]
    assert artifact.is_file()
    assert gate._sha256_file(artifact) == wheel["builds"][0]["sha256"]
    assert artifact.name.startswith(f"scholialang_mcp-{RELEASE}-")

    mcpb = report["artifacts"]["mcpb"]
    assert mcpb["staged"] is True
    assert mcpb["manifest_version"] == RELEASE
    assert mcpb["staged_tree_sha256"]
    assert mcpb["native_skill_surface"] is False
    assert mcpb["skill_md_in_bundle"] is False
    assert mcpb["packed"] is False and "pack_skipped_reason" in mcpb

    assert report["artifacts"]["built_from"].startswith("git archive HEAD")
    assert report["overall"]["verdict"] == "pass", report["overall"]["reasons"]
