#!/usr/bin/env python3
"""No-publish release gate: machine-readable evidence, zero side effects.

Collects everything a release decision needs — source commit, every version
axis, skill-materialization freshness, clean-checkout artifact builds with
reproducible hashes, required test-suite verdicts, residual risks, and a
non-binding version recommendation — into one ``release_gate.json``, and then
STOPS. It never tags, uploads to PyPI, creates a GitHub release, updates a
marketplace, installs globally, writes the default branch, or mutates any
version number; every publication gate is recorded as ``performed: false``.

Guarantees:

* Artifacts are built from a pristine ``git archive HEAD`` checkout in a
  temporary directory, never from the working tree; the wheel is built twice
  from two independent clean checkouts and the hashes must agree.
* The report is deterministic for a given commit and outcome set: no
  timestamps, sorted keys, stable ordering.
* The only filesystem writes are the report and artifact copies under the
  operator-chosen ``--output-dir`` (plus throwaway temporary directories).
* A version bump is never chosen or applied. The recommendation names the
  mechanical facts (current version, released tag, commits since) and leaves
  the decision to explicit release approval.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from pathlib import Path

GATE_VERSION = "0.7.3"
SCHEMA_VERSION = 1
STABLE_GRAMMAR_VERSION = "0.6.2"

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ("scholialang-doctor", "scholialang-verify")
VENDOR_HOSTS = ("claude-code", "codex", "ollama")

# Every publication side effect this gate deliberately stops before.
PUBLICATION_GATES = (
    "git_tag",
    "github_release",
    "pypi_upload",
    "marketplace_update",
    "mcpb_distribution",
    "global_install",
    "default_branch_write",
)

# Required conformance suites: the new skill tests plus the existing
# release-version, public-hygiene, MCP protocol/support, vendored-contract,
# and .mcpb gates.
REQUIRED_SUITES = (
    {
        "id": "skills",
        "paths": [
            "tests/test_scholia_doctor.py",
            "tests/test_scholia_verify.py",
            "tests/test_skill_materialization.py",
            "tests/test_release_gate.py",
        ],
    },
    {"id": "release_versions", "paths": ["tests/test_release_versions.py"]},
    {"id": "public_hygiene", "paths": ["tests/test_public_hygiene.py"]},
    {
        "id": "mcp_protocol_support",
        "paths": [
            "tests/integration/test_mcp_protocol.py",
            "tests/integration/test_mcp_support_matrix.py",
            "tests/integration/test_mcp_2026_conformance.py",
            "tests/integration/test_mcp_plugin_2026_conformance.py",
        ],
    },
    {"id": "vendored_contract", "paths": ["tests/test_v062_vendor_contract.py"]},
    {"id": "claude_desktop_mcpb", "paths": ["tests/test_claude_desktop_mcpb.py"]},
)
SUITE_TIMEOUT = 1800


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _load_script_module(name: str, root: Path):
    spec = importlib.util.spec_from_file_location(name, root / "scripts" / f"{name}.py")
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load scripts/{name}.py from {root}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    ).stdout.strip()


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Evidence sections
# ---------------------------------------------------------------------------


def source_section(root: Path) -> dict:
    return {
        "commit": _git(root, "rev-parse", "HEAD"),
        "branch": _git(root, "rev-parse", "--abbrev-ref", "HEAD"),
        "dirty_working_tree": bool(_git(root, "status", "--porcelain")),
        "tags_at_head": sorted(filter(None, _git(root, "tag", "--points-at", "HEAD").splitlines())),
    }


def version_axes_section(root: Path) -> dict:
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    doctor = _load_script_module("materialize_skills", root)

    def skill_meta(skill: str) -> dict:
        text = (root / doctor.CANONICAL_SKILLS_DIR / skill / "SKILL.md").read_text(encoding="utf-8")
        block = doctor._frontmatter_block(text) or ""
        version = doctor._META_VERSION_RE.search(block)
        grammar = doctor._META_GRAMMAR_RE.search(block)
        return {
            "version": version.group(1) if version else None,
            "grammar": grammar.group(1) if grammar else None,
        }

    axes = {
        "package": project["version"],
        "grammar": STABLE_GRAMMAR_VERSION,
        "grammar_corpus_present": (
            root / "tests/fixtures/scholialang-spec" / f"v{STABLE_GRAMMAR_VERSION}"
        ).is_dir(),
        "marketplace": _read_json(root / ".claude-plugin/marketplace.json")["metadata"]["version"],
        "plugins": {
            "claude-code": _read_json(
                root / "plugins/claude-code/scholialang/.claude-plugin/plugin.json"
            )["version"],
            "codex": _read_json(
                root / "plugins/codex/scholialang/.codex-plugin/plugin.json"
            )["version"],
            "claude-desktop": _read_json(
                root / "plugins/claude-desktop/scholialang/manifest.json"
            )["version"],
        },
        "vendored_validator": {
            host: _read_json(
                root / "plugins" / host / "scholialang/scripts/_scholia_vendored/UPSTREAM.json"
            ).get("validator_version")
            for host in VENDOR_HOSTS
        },
        "skills": {skill: skill_meta(skill) for skill in SKILLS},
    }
    release_values = {
        axes["package"],
        axes["marketplace"],
        *axes["plugins"].values(),
        *axes["vendored_validator"].values(),
        *(meta["version"] for meta in axes["skills"].values()),
    }
    grammar_values = {axes["grammar"], *(meta["grammar"] for meta in axes["skills"].values())}
    axes["aligned"] = (
        len(release_values) == 1
        and grammar_values == {STABLE_GRAMMAR_VERSION}
        and axes["grammar_corpus_present"]
    )
    return axes


def materialization_section(root: Path) -> dict:
    materializer = _load_script_module("materialize_skills", root)
    issues = materializer.check(root)
    first = materializer.render_all(root)
    second = materializer.render_all(root)
    return {
        "fresh": not issues,
        "deterministic": first == second,
        "issues": issues,
        "hosts": [dict(entry) for entry in materializer.HOST_MATRIX],
    }


def _clean_checkout(root: Path, destination: Path) -> None:
    """Extract ``git archive HEAD`` — never the working tree — into destination."""
    destination.mkdir(parents=True, exist_ok=True)
    archive = subprocess.run(
        ["git", "archive", "--format=tar", "HEAD"],
        cwd=root,
        capture_output=True,
        check=True,
        timeout=120,
    ).stdout
    with tempfile.TemporaryFile() as handle:
        handle.write(archive)
        handle.seek(0)
        with tarfile.open(fileobj=handle) as tar:
            tar.extractall(destination, filter="data")


def _build_wheel(clean_tree: Path, out_dir: Path, *, source_epoch: str) -> tuple[Path | None, list[str]]:
    """``pip wheel`` with the same interpreter fallback the verify suite uses:
    layered ephemeral environments can leak a pre-PEP-639 setuptools.

    ``SOURCE_DATE_EPOCH`` is pinned to the source commit time so the generated
    dist-info zip entries carry reproducible timestamps."""
    candidates = [sys.executable, shutil.which("python3", path="/usr/local/bin:/usr/bin:/bin")]
    attempts: list[str] = []
    for python in dict.fromkeys(filter(None, candidates)):
        completed = subprocess.run(
            [python, "-m", "pip", "wheel", "--no-deps", "-w", str(out_dir), str(clean_tree)],
            env=dict(os.environ, SOURCE_DATE_EPOCH=source_epoch),
            capture_output=True,
            text=True,
            timeout=300,
        )
        if completed.returncode == 0:
            wheels = sorted(out_dir.glob("scholialang_mcp-*.whl"))
            if wheels:
                return wheels[0], attempts
        attempts.append(f"{python}: {completed.stderr[-400:]}")
    return None, attempts


def _staged_tree_digest(staged: Path) -> str:
    entries = "".join(
        f"{path.relative_to(staged).as_posix()}:{_sha256_file(path)}\n"
        for path in sorted(staged.rglob("*"))
        if path.is_file()
    )
    return _sha256_bytes(entries.encode("utf-8"))


def artifacts_section(root: Path, output_dir: Path, *, mcpb_pack: bool) -> dict:
    """Build the release artifacts from two pristine checkouts and hash them."""
    section: dict = {"built_from": "git archive HEAD (clean isolated checkout)"}
    source_epoch = _git(root, "log", "-1", "--format=%ct", "HEAD")
    wheel_builds: list[dict] = []
    final_wheel: Path | None = None
    with tempfile.TemporaryDirectory(prefix="release-gate-") as tmp:
        for attempt in ("first", "second"):
            clean_tree = Path(tmp) / attempt / "src-tree"
            _clean_checkout(root, clean_tree)
            wheel, errors = _build_wheel(
                clean_tree, Path(tmp) / attempt / "wheelhouse", source_epoch=source_epoch
            )
            wheel_builds.append(
                {
                    "checkout": attempt,
                    "built": wheel is not None,
                    "filename": wheel.name if wheel else None,
                    "sha256": _sha256_file(wheel) if wheel else None,
                    "errors": errors,
                }
            )
            final_wheel = wheel or final_wheel

        hashes = {build["sha256"] for build in wheel_builds}
        section["wheel"] = {
            "builds": wheel_builds,
            "reproducible": len(hashes) == 1 and None not in hashes,
        }
        if final_wheel is not None:
            artifact_dir = output_dir / "artifacts"
            artifact_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(final_wheel, artifact_dir / final_wheel.name)
            section["wheel"]["artifact"] = f"artifacts/{final_wheel.name}"

        mcpb_tree = Path(tmp) / "first" / "src-tree"
        builder = _load_script_module("build_claude_desktop_mcpb", mcpb_tree)
        staged = Path(tmp) / "mcpb-staged" / "scholialang"
        try:
            manifest = builder.stage_bundle(staged)
            mcpb: dict = {
                "staged": True,
                "manifest_version": manifest["version"],
                "staged_tree_sha256": _staged_tree_digest(staged),
                "native_skill_surface": False,
                "skill_md_in_bundle": bool(list(staged.rglob("SKILL.md"))),
            }
        except Exception as error:  # staged honestly, never silently
            mcpb = {"staged": False, "error": str(error), "native_skill_surface": False}
        if mcpb.get("staged") and mcpb_pack:
            packed = (
                output_dir
                / "artifacts"
                / f"scholialang-claude-desktop-{mcpb['manifest_version']}.mcpb"
            )
            packed.parent.mkdir(parents=True, exist_ok=True)
            try:
                digest = builder.build(packed)
                mcpb["packed"] = True
                mcpb["packed_sha256"] = digest
                mcpb["artifact"] = f"artifacts/{packed.name}"
            except Exception as error:
                mcpb["packed"] = False
                mcpb["pack_error"] = str(error)
        elif mcpb.get("staged"):
            mcpb["packed"] = False
            mcpb["pack_skipped_reason"] = (
                "npx pack not requested (--mcpb-pack); staged tree digest recorded instead"
            )
        section["mcpb"] = mcpb
    return section


def run_suites(root: Path, suites) -> list[dict]:
    env = dict(os.environ, PYTHONPATH=str(root / "src"))
    results: list[dict] = []
    for suite in suites:
        command = suite.get("command") or [
            sys.executable,
            "-m",
            "pytest",
            *suite["paths"],
            "--timeout=300",
        ]
        try:
            completed = subprocess.run(
                command,
                cwd=root,
                env=env,
                capture_output=True,
                text=True,
                timeout=SUITE_TIMEOUT,
            )
            returncode = completed.returncode
            tail = "" if returncode == 0 else (completed.stdout + completed.stderr)[-2000:]
        except subprocess.TimeoutExpired:
            returncode, tail = -1, f"timed out after {SUITE_TIMEOUT}s"
        results.append(
            {
                "id": suite["id"],
                "command": [str(part) for part in command],
                "required": suite.get("required", True),
                "status": "pass" if returncode == 0 else "fail",
                "returncode": returncode,
                "output_tail": tail,
            }
        )
    return results


def version_recommendation_section(root: Path, package_version: str) -> dict:
    tags = set(_git(root, "tag", "--list").splitlines())
    released_tag = f"v{package_version}" if f"v{package_version}" in tags else None
    commits_since = None
    if released_tag:
        commits_since = int(_git(root, "rev-list", "--count", f"{released_tag}..HEAD"))
    if released_tag is None:
        action = "release_current_version"
        rationale = (
            f"pyproject version {package_version} has no released tag; the current "
            "version number is publishable as-is once the gate passes."
        )
    elif commits_since == 0:
        action = "none"
        rationale = f"HEAD is exactly the released tag {released_tag}; nothing to publish."
    else:
        action = "bump_before_publish"
        rationale = (
            f"tag {released_tag} is already released and HEAD carries {commits_since} "
            "newer commit(s); publishing requires a version bump. Repository policy "
            "does not mechanically decide between a patch and a minor bump for new "
            "public skill surfaces, so no version is chosen here."
        )
    return {
        "binding": False,
        "applied": False,
        "mechanically_unambiguous": released_tag is None or commits_since == 0,
        "current_version": package_version,
        "released_tag": released_tag,
        "commits_since_released_tag": commits_since,
        "action": action,
        "rationale": rationale,
    }


def _derive_overall(report: dict, *, artifacts_skipped: bool, suites_skipped: bool) -> dict:
    reasons: list[str] = []
    verdict = "pass"

    def fail(message: str) -> None:
        nonlocal verdict
        verdict = "fail"
        reasons.append(message)

    def incomplete(message: str) -> None:
        nonlocal verdict
        if verdict != "fail":
            verdict = "incomplete"
        reasons.append(message)

    if not report["version_axes"]["aligned"]:
        fail("version axes are not aligned")
    if not report["materialization"]["fresh"]:
        fail("skill materialization is stale or invalid")
    if not report["materialization"]["deterministic"]:
        fail("skill materialization is not deterministic")
    if artifacts_skipped:
        incomplete("artifact builds were skipped by operator request")
    else:
        if not report["artifacts"]["wheel"]["reproducible"]:
            fail("wheel build missing or not reproducible across clean checkouts")
        if not report["artifacts"]["mcpb"].get("staged"):
            fail("claude-desktop mcpb bundle failed to stage")
    if suites_skipped:
        incomplete("required test suites were skipped by operator request")
    for suite in report["tests"]["suites"]:
        if suite["required"] and suite["status"] != "pass":
            fail(f"required suite {suite['id']} did not pass")
    return {"verdict": verdict, "reasons": reasons}


def build_gate_report(
    root: Path,
    output_dir: Path,
    *,
    suites=REQUIRED_SUITES,
    build_artifacts: bool = True,
    mcpb_pack: bool = False,
) -> dict:
    root = root.resolve()
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    axes = version_axes_section(root)
    report: dict = {
        "schema_version": SCHEMA_VERSION,
        "gate_version": GATE_VERSION,
        "policy": {
            "no_publish": True,
            "description": (
                "evidence-only gate: publication, tagging, uploads, marketplace "
                "updates, global installs, and version mutation all require "
                "explicit operator release approval"
            ),
        },
        "source": source_section(root),
        "version_axes": axes,
        "materialization": materialization_section(root),
        "artifacts": (
            artifacts_section(root, output_dir, mcpb_pack=mcpb_pack)
            if build_artifacts
            else {"skipped": True, "reason": "skipped by operator request"}
        ),
        "tests": {"suites": run_suites(root, suites)},
        "version_recommendation": version_recommendation_section(root, axes["package"]),
        "publication": [
            {"gate": gate, "performed": False, "blocked_on": "explicit operator release approval"}
            for gate in PUBLICATION_GATES
        ],
        "residual_risks": [
            "claude-desktop and ollama have no native skill surface; skill semantics "
            "reach them only through MCP capabilities (native_skill_surface=false)",
            "the .mcpb pack step requires npx and is only hash-verified when "
            "--mcpb-pack is requested; the staged tree digest is the default evidence",
            "the version bump for the next release is not mechanically unambiguous "
            "and is deliberately left unchosen",
        ],
    }
    report["overall"] = _derive_overall(
        report,
        artifacts_skipped=not build_artifacts,
        suites_skipped=suites is not REQUIRED_SUITES and not suites,
    )
    payload = json.dumps(report, indent=2, sort_keys=True) + "\n"
    (output_dir / "release_gate.json").write_text(payload, encoding="utf-8")
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="No-publish release gate: collect evidence, then stop."
    )
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "dist" / "release-gate",
        help="where release_gate.json and artifact copies are written",
    )
    parser.add_argument(
        "--skip-suites",
        action="store_true",
        help="record suites as skipped instead of running them (verdict: incomplete)",
    )
    parser.add_argument(
        "--skip-artifacts",
        action="store_true",
        help="skip clean-checkout artifact builds (verdict: incomplete)",
    )
    parser.add_argument(
        "--mcpb-pack",
        action="store_true",
        help="also pack the .mcpb via npx and record its hash",
    )
    parser.add_argument("--json", action="store_true", help="print the report to stdout")
    args = parser.parse_args(argv)

    report = build_gate_report(
        args.root,
        args.output_dir,
        suites=() if args.skip_suites else REQUIRED_SUITES,
        build_artifacts=not args.skip_artifacts,
        mcpb_pack=args.mcpb_pack,
    )
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(f"verdict: {report['overall']['verdict']}")
        for reason in report["overall"]["reasons"]:
            print(f"  - {reason}")
        print(f"report: {args.output_dir / 'release_gate.json'}")
    return {"pass": 0, "incomplete": 1}.get(report["overall"]["verdict"], 2)


if __name__ == "__main__":
    sys.exit(main())
