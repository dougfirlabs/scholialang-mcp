"""Contract tests for the isolated scholialang-verify skill runner.

Covers the PRD gates: the scenario manifest declares positive, negative,
composition, and installed-artifact classes for every feature family in
scope; the verdict is derived from required scenarios only and a skipped
required scenario is never a pass; evidence is bounded, redacted, and
byte-stable across repeated runs; the runner survives a hostile CWD and
environment; and a sentinel real DAG store is byte-unmodified by a full run.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
CANONICAL_SKILL = ROOT / "plugins" / "claude-code" / "scholialang" / "skills" / "scholialang-verify"
CODEX_SKILL = ROOT / "plugins" / "codex" / "scholialang" / "skills" / "scholialang-verify"
VERIFY_SCRIPT = CANONICAL_SKILL / "scripts" / "scholia_verify.py"

_SPEC = importlib.util.spec_from_file_location("scholia_verify", VERIFY_SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
verify = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(verify)

RELEASE = "0.7.3"
GRAMMAR = "0.7.0"

PLUGIN_SCENARIO_IDS = [s["id"] for s in verify.MANIFEST if "canonical-plugin" in s["arms"]]
INSTALLED_SCENARIO_IDS = [s["id"] for s in verify.MANIFEST if s["arms"] == ["installed-wheel"]]


def _row(report, arm, scenario_id):
    matches = [r for r in report["results"] if r["arm"] == arm and r["scenario_id"] == scenario_id]
    assert len(matches) == 1, f"expected exactly one result for {arm}/{scenario_id}"
    return matches[0]


def _result(required=True, status="pass", arm="canonical-plugin", scenario_id="s", reason=""):
    return {"arm": arm, "scenario_id": scenario_id, "required": required, "status": status, "reason": reason}


# ---------------------------------------------------------------------------
# Story S1: the manifest and verdict contract.
# ---------------------------------------------------------------------------


def test_manifest_declares_all_four_scenario_classes():
    classes = {scenario["class"] for scenario in verify.MANIFEST}
    assert {"positive", "negative", "composition", "installed"} <= classes
    ids = [scenario["id"] for scenario in verify.MANIFEST]
    assert len(ids) == len(set(ids))
    for scenario in verify.MANIFEST:
        assert {"id", "title", "family", "class", "boundary", "arms", "required", "expected", "evidence_file"} <= set(scenario)
        assert set(scenario["arms"]) <= set(verify.ALL_ARMS)
        assert scenario["arms"], scenario["id"]


def test_manifest_pairs_positives_and_negatives_per_feature_family():
    by_family: dict[str, set[str]] = {}
    for scenario in verify.MANIFEST:
        by_family.setdefault(scenario["family"], set()).add(scenario["class"])
    # Every advertised lint-level family carries both directions.
    for family in ("catalog", "goal_concluding", "action_finding", "hypothesis_evidence_finding"):
        assert {"positive", "negative"} <= by_family[family], family
    assert "negative" in by_family["invalid_input"]
    assert "composition" in by_family["dag_lifecycle"]
    assert "composition" in by_family["dag_query"]
    assert "composition" in by_family["shared_fixtures"]
    assert {"installed", "negative"} <= by_family["installed_artifact"]


def test_verdict_requires_every_required_scenario_to_pass():
    all_pass = [_result(scenario_id="a"), _result(scenario_id="b")]
    assert verify.derive_verdict(all_pass)["verdict"] == "pass"

    one_fail = [_result(scenario_id="a"), _result(scenario_id="b", status="fail", reason="boom")]
    overall = verify.derive_verdict(one_fail)
    assert overall["verdict"] == "fail"
    assert overall["required_passed"] == 1
    assert overall["reasons"][0]["scenario_id"] == "b"


@pytest.mark.parametrize("status", ["not_run", "unsupported"])
def test_skipped_or_unsupported_required_scenario_is_never_pass(status):
    results = [_result(scenario_id="a"), _result(scenario_id="b", status=status)]
    overall = verify.derive_verdict(results)
    assert overall["verdict"] == "incomplete"
    assert overall["required_passed"] == 1


def test_optional_failures_are_reported_without_flipping_the_verdict():
    results = [_result(scenario_id="a"), _result(scenario_id="opt", required=False, status="fail", reason="x")]
    overall = verify.derive_verdict(results)
    assert overall["verdict"] == "pass"
    assert overall["optional_failures"][0]["scenario_id"] == "opt"


def test_manifest_listing_is_side_effect_free(tmp_path, capsys):
    assert verify.main(["--list", "--evidence-dir", str(tmp_path / "unused")]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert [s["id"] for s in listed["scenarios"]] == [s["id"] for s in verify.MANIFEST]
    assert not (tmp_path / "unused").exists()


# ---------------------------------------------------------------------------
# Story S2: the runner against the real public boundaries.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def wheel_fixture(tmp_path_factory) -> Path:
    """One real release wheel built from this source tree.

    Layered ephemeral test environments (e.g. ``uv run --with``) can leak a
    pre-PEP-639 setuptools into pip's build backend, so fall back to the base
    interpreter when the test interpreter cannot build the wheel.
    """
    import shutil

    out = tmp_path_factory.mktemp("wheel")
    candidates = [sys.executable, shutil.which("python3", path="/usr/local/bin:/usr/bin:/bin")]
    attempts = []
    for python in dict.fromkeys(filter(None, candidates)):
        completed = subprocess.run(
            [python, "-m", "pip", "wheel", "--no-deps", "-w", str(out), str(ROOT)],
            capture_output=True,
            text=True,
            timeout=300,
        )
        if completed.returncode == 0:
            break
        attempts.append(f"{python}: {completed.stderr[-400:]}")
    wheels = sorted(out.glob("scholialang_mcp-*.whl"))
    assert wheels, "pip wheel produced no artifact:\n" + "\n".join(attempts)
    return wheels[0]


@pytest.fixture(scope="session")
def battery_report(tmp_path_factory):
    """One full run over both plugin arms; several tests read it."""
    evidence = tmp_path_factory.mktemp("battery-evidence")
    report = verify.build_report(
        root=ROOT,
        evidence_dir=evidence,
        arms=("canonical-plugin", "vendored-codex"),
    )
    return report, evidence


@pytest.mark.timeout(300)
def test_both_plugin_arms_pass_the_full_required_battery(battery_report):
    report, evidence = battery_report
    assert report["overall"]["verdict"] == "pass"
    assert report["overall"]["required_passed"] == report["overall"]["required_total"] == 2 * len(PLUGIN_SCENARIO_IDS)
    for arm in ("canonical-plugin", "vendored-codex"):
        for scenario_id in PLUGIN_SCENARIO_IDS:
            row = _row(report, arm, scenario_id)
            assert row["status"] == "pass", f"{arm}/{scenario_id}: {row['reason']}"
            assert (evidence / arm / f"{scenario_id}.json").is_file()
    assert (evidence / "verify_report.json").is_file()


@pytest.mark.timeout(60)
def test_evidence_is_bounded_normalized_and_schema_stable(battery_report):
    _, evidence = battery_report
    for path in sorted((evidence / "canonical-plugin").glob("*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["schema_version"] == verify.SCHEMA_VERSION
        assert payload["status"] == "pass"
        assert payload["checks"], path.name
        assert payload["exchanges"], path.name
        text = path.read_text(encoding="utf-8")
        assert str(evidence) not in text, "evidence leaked an absolute sandbox path"
        assert not re.search(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", text), "evidence leaked a timestamp"
        assert not re.search(r"dag_\d{8}T\d{6}Z_[0-9a-f]{8}", text), "evidence leaked a minted DAG id"
        for exchange in payload["exchanges"]:
            if exchange.get("truncated"):
                assert len(exchange["head"]) <= 1200 and exchange["sha256"]


@pytest.mark.timeout(60)
def test_lifecycle_evidence_proves_the_export_revalidates(battery_report):
    _, evidence = battery_report
    payload = json.loads((evidence / "canonical-plugin" / "dag_lifecycle_roundtrip.json").read_text(encoding="utf-8"))
    checks = {check["name"]: check["ok"] for check in payload["checks"]}
    for name in ("json_export_parses", "json_export_has_all_atoms", "xml_export_lints_clean"):
        assert checks[name] is True, name


@pytest.mark.timeout(60)
def test_shared_corpus_cases_are_driven_not_retyped(battery_report):
    _, evidence = battery_report
    payload = json.loads((evidence / "canonical-plugin" / "shared_spec_fixtures.json").read_text(encoding="utf-8"))
    case_checks = [check for check in payload["checks"] if check["name"].startswith("case_v062-")]
    corpus = json.loads((ROOT / "tests/fixtures/scholialang-spec/v0.6.2/action_recorded.json").read_text(encoding="utf-8"))
    trace_only_cases = [case for case in corpus["cases"] if not case.get("graph_edges")]
    assert len(case_checks) == len(trace_only_cases)
    assert all(check["ok"] for check in case_checks)


@pytest.mark.timeout(120)
def test_missing_shared_corpus_is_reported_never_silently_passed(tmp_path):
    skips = [s for s in PLUGIN_SCENARIO_IDS if s != "shared_spec_fixtures"]
    report = verify.build_report(
        root=ROOT,
        evidence_dir=tmp_path / "evidence",
        arms=("canonical-plugin",),
        fixtures_dir=tmp_path / "no-corpus-here",
        skip_ids=skips,
    )
    row = _row(report, "canonical-plugin", "shared_spec_fixtures")
    assert row["status"] == "not_run"
    assert "unavailable" in row["reason"]
    assert report["overall"]["verdict"] == "incomplete"


@pytest.mark.timeout(300)
def test_installed_arm_passes_from_a_clean_wheel_fixture(tmp_path, wheel_fixture):
    report = verify.build_report(
        root=ROOT,
        evidence_dir=tmp_path / "evidence",
        arms=("installed-wheel",),
        wheel=wheel_fixture,
    )
    assert report["overall"]["verdict"] == "pass", report["overall"]["reasons"]
    for scenario_id in INSTALLED_SCENARIO_IDS:
        assert _row(report, "installed-wheel", scenario_id)["status"] == "pass"
    resolved = json.loads((tmp_path / "evidence/installed-wheel/installed_wheel_resolved.json").read_text(encoding="utf-8"))
    checks = {check["name"]: check["ok"] for check in resolved["checks"]}
    assert checks["wheel_version_matches_source"] is True


@pytest.mark.timeout(60)
def test_missing_wheel_fixture_fails_closed_and_downstream_does_not_run(tmp_path):
    report = verify.build_report(
        root=ROOT,
        evidence_dir=tmp_path / "evidence",
        arms=("installed-wheel",),
        wheel=tmp_path / "no-such.whl",
    )
    assert report["overall"]["verdict"] == "fail"
    assert _row(report, "installed-wheel", "installed_wheel_resolved")["status"] == "fail"
    for scenario_id in ("installed_wheel_clean_install", "installed_server_protocol", "installed_disabled_mode_refuses"):
        assert _row(report, "installed-wheel", scenario_id)["status"] == "not_run"


@pytest.mark.timeout(120)
def test_operator_skip_of_a_required_scenario_exits_incomplete(tmp_path):
    argv = ["--root", str(ROOT), "--evidence-dir", str(tmp_path / "evidence"), "--arm", "canonical-plugin", "--json"]
    for scenario_id in PLUGIN_SCENARIO_IDS:
        if scenario_id != "goal_concluding_positive":
            argv += ["--skip", scenario_id]
    assert verify.main(argv) == 1
    payload = json.loads((tmp_path / "evidence/canonical-plugin/catalog_completeness.json").read_text(encoding="utf-8"))
    assert payload["status"] == "not_run"
    assert payload["reason"] == "skipped by operator request"


# ---------------------------------------------------------------------------
# Story S3: determinism, redaction, and isolation from the real store.
# ---------------------------------------------------------------------------

_DETERMINISM_SCENARIOS = (
    "dag_lifecycle_roundtrip",
    "session_finish_roundtrip",
    "search_neighbors_compaction",
    "shared_spec_fixtures",
)


def _evidence_bytes(evidence_dir: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(evidence_dir)): path.read_bytes()
        for path in sorted(evidence_dir.rglob("*"))
        if path.is_file()
    }


@pytest.mark.timeout(120)
def test_repeated_runs_produce_byte_identical_evidence(tmp_path):
    skips = [s for s in PLUGIN_SCENARIO_IDS if s not in _DETERMINISM_SCENARIOS]
    trees = []
    for run in ("first", "second"):
        evidence = tmp_path / run
        verify.build_report(root=ROOT, evidence_dir=evidence, arms=("canonical-plugin",), skip_ids=skips)
        trees.append(_evidence_bytes(evidence))
    assert trees[0] == trees[1]


def _sentinel_home(tmp_path: Path) -> tuple[Path, bytes]:
    home = tmp_path / "fake-home"
    store = home / ".scholialang"
    store.mkdir(parents=True)
    sentinel = b"SENTINEL-REAL-STORE-BYTES\x00" * 64
    (store / "scholialang.sqlite3").write_bytes(sentinel)
    return home, sentinel


@pytest.mark.timeout(300)
def test_full_cli_run_is_isolated_redacted_and_hostile_proof(tmp_path):
    """One adversarial end-to-end CLI run pins four gates at once.

    A sentinel real store (via both HOME and a hostile SCHOLIALANG_HOME),
    secret canary values in the environment, and stdlib-shadowing files in
    the CWD must not affect the run, leak into evidence, or be modified.
    """
    home, sentinel = _sentinel_home(tmp_path)
    hostile_cwd = tmp_path / "hostile-cwd"
    hostile_cwd.mkdir()
    canary_file = hostile_cwd / "canary.txt"
    payload = f"open({str(canary_file)!r}, 'w').write('boom')\n"
    for name in ("json.py", "tomllib.py", "subprocess.py"):
        (hostile_cwd / name).write_text(payload, encoding="utf-8")

    evidence = tmp_path / "evidence"
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "SCHOLIALANG_HOME": str(home / ".scholialang"),
        "EXAMPLE_API_TOKEN": "hunter2-canary-value",
        "SCHOLIA_SESSION_ID": "ambient-session-should-not-matter",
    }
    result = subprocess.run(
        [sys.executable, str(VERIFY_SCRIPT), "--root", str(ROOT), "--evidence-dir", str(evidence), "--arm", "canonical-plugin", "--json"],
        cwd=hostile_cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=280,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)["overall"]["verdict"] == "pass"
    assert not canary_file.exists(), "the runner imported code from the untrusted CWD"
    assert (home / ".scholialang" / "scholialang.sqlite3").read_bytes() == sentinel, (
        "the real DAG store sentinel was modified"
    )
    assert sorted(p.name for p in (home / ".scholialang").iterdir()) == ["scholialang.sqlite3"], (
        "the runner wrote extra files into the real store directory"
    )
    for haystack in (result.stdout, result.stderr):
        assert "hunter2-canary-value" not in haystack
    for path in evidence.rglob("*.json"):
        assert "hunter2-canary-value" not in path.read_text(encoding="utf-8"), path


@pytest.mark.timeout(60)
def test_exit_code_two_on_required_failure(tmp_path):
    code = verify.main(
        [
            "--root",
            str(ROOT),
            "--evidence-dir",
            str(tmp_path / "evidence"),
            "--arm",
            "installed-wheel",
            "--wheel",
            str(tmp_path / "missing.whl"),
            "--json",
        ]
    )
    assert code == 2


# ---------------------------------------------------------------------------
# The shipped skill: hygiene and host-copy parity.
# ---------------------------------------------------------------------------


def _skill_files(skill_dir: Path) -> list[Path]:
    return sorted(
        path.relative_to(skill_dir)
        for path in skill_dir.rglob("*")
        if path.is_file()
        and "__pycache__" not in path.relative_to(skill_dir).parts
        and path.suffix != ".pyc"
    )


def test_shipped_skill_is_concise_and_has_no_auxiliary_files():
    forbidden = {"README.md", "INSTALLATION_GUIDE.md", "CHANGELOG.md"}
    for skill_dir in (CANONICAL_SKILL, CODEX_SKILL):
        names = {path.name for path in skill_dir.rglob("*") if path.is_file()}
        assert not names & forbidden, f"auxiliary docs found in {skill_dir}"
        assert "SKILL.md" in names


def test_codex_host_copy_is_byte_identical_generated_artifact():
    canonical_files = _skill_files(CANONICAL_SKILL)
    assert canonical_files == _skill_files(CODEX_SKILL), (
        "Codex host copy drifted from the canonical skill; run scripts/sync_plugins.sh"
    )
    for rel in canonical_files:
        assert (CANONICAL_SKILL / rel).read_bytes() == (CODEX_SKILL / rel).read_bytes(), rel


def test_skill_frontmatter_declares_release_and_grammar_axes():
    text = (CANONICAL_SKILL / "SKILL.md").read_text(encoding="utf-8")
    match = re.match(r"\A---\n(.*?)\n---", text, re.DOTALL)
    assert match is not None
    block = match.group(1)
    top_level_keys = {
        line.split(":", 1)[0].strip()
        for line in block.splitlines()
        if line and not line.startswith((" ", "\t")) and ":" in line
    }
    allowed = {"name", "description", "license", "allowed-tools", "metadata", "compatibility"}
    assert {"name", "description"} <= top_level_keys <= allowed
    assert re.search(r'^\s+version:\s*"0\.7\.3"\s*$', block, re.MULTILINE)
    assert re.search(r'^\s+grammar:\s*"0\.7\.0"\s*$', block, re.MULTILINE)
    assert verify.VERIFY_VERSION == RELEASE
    assert verify.STABLE_GRAMMAR_VERSION == GRAMMAR
