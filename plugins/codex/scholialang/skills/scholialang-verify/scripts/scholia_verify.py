#!/usr/bin/env python3
"""Isolated end-to-end verifier for the public Scholialang contract.

Version parity does not prove functionality. This runner executes an explicit
scenario manifest — positive, negative, composition, and installed-artifact
classes for every advertised feature family in scope — against the *public*
boundaries (the plugin MCP servers over stdio JSON-RPC and the installed
wheel's ``python -m scholialang_mcp`` entry), and derives an honest overall
verdict from required scenarios only.

Guarantees:

* Isolation. Every scenario runs against throwaway sandbox directories: a
  private ``HOME``, a private ``SCHOLIALANG_HOME`` DAG store, a private
  project directory, and (for the installed arm) a private ``--target``
  site directory. The user's real ``~/.scholialang`` database, plugin
  cache, and project traces are never opened, read, or written.
* Public boundary. Scenarios speak JSON-RPC to real server subprocesses or
  run real ``pip`` installs; no scenario imports project code and then
  claims protocol functionality.
* Honest statuses. Each scenario ends ``pass``, ``fail``, ``unsupported``,
  or ``not_run``. A skipped or skipped-over required scenario is never a
  pass; the overall verdict is ``pass`` only when every required scenario
  in the selected arms passed.
* Bounded, redacted, deterministic evidence. Raw request/response
  exchanges are preserved per scenario, but sandbox paths, timestamps, and
  minted DAG ids are normalized, oversized payloads are truncated with a
  digest, and values of secret-looking environment variables are scrubbed.
  Environment values are never dumped.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

VERIFY_VERSION = "0.7.2"
SCHEMA_VERSION = 1
# The stable Scholia language grammar. Release 0.7.2 implements grammar
# v0.6.2 — distinct axes, expected alignment, never a downgrade.
STABLE_GRAMMAR_VERSION = "0.6.2"
SKILL_NAME = "scholialang-verify"

MCP_PACKAGE_DIST = "scholialang-mcp"
CORE_ATOM_KINDS = (
    "Goal",
    "Thinking",
    "Observation",
    "Action",
    "Hypothesis",
    "Evidence",
    "Finding",
    "Concluding",
    "Constraint",
)
CANONICAL_OPERATOR_COUNT = 11
WHEEL_SERVER_TOOLS = {"lookup_file_summary", "get_tree"}
WHEEL_SERVER_TOOL_COUNT = 8

PLUGIN_SERVERS = {
    "canonical-plugin": Path("plugins/claude-code/scholialang/scripts/scholialang_mcp_server.py"),
    "vendored-codex": Path("plugins/codex/scholialang/scripts/scholialang_mcp_server.py"),
    "vendored-ollama": Path("plugins/ollama/scholialang/scripts/scholialang_mcp_server.py"),
}
PLUGIN_ARMS = tuple(PLUGIN_SERVERS)
INSTALLED_ARM = "installed-wheel"
ALL_ARMS = PLUGIN_ARMS + (INSTALLED_ARM,)
DEFAULT_ARMS = ("canonical-plugin", "vendored-codex", INSTALLED_ARM)

PLUGIN_PROTOCOL_VERSION = "2025-11-25"
WHEEL_PROTOCOL_VERSION = "2025-06-18"
SPEC_FIXTURE_SUBDIR = Path("tests/fixtures/scholialang-spec") / f"v{STABLE_GRAMMAR_VERSION}"
SPEC_FIXTURE_FILE = "action_recorded.json"

EXCHANGE_CHAR_BUDGET = 4000
SERVER_TIMEOUT_S = 30
PIP_TIMEOUT_S = 300

SECRET_ENV_RE = re.compile(r"(?i)(token|secret|password|passwd|credential|api[_-]?key|private[_-]?key)")
_TIMESTAMP_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|\+00:00)")
_DAG_ID_RE = re.compile(r"dag_\d{8}T\d{6}Z_[0-9a-f]{8}")
# project_key is a digest the server derives from the absolute sandbox path,
# so it changes run to run even though every path is normalized.
_PROJECT_KEY_RE = re.compile(r'project_key[\\"]*\s*:\s*[\\"]*([0-9a-f]{8,64})')

# Deterministic inline traces: the positive/negative pair for every lint-level
# feature family. The composition families are exercised live over the DAG
# tools instead of by inline text.
TRACES = {
    "goal_concluding_positive": (
        '<Step id="S_01">'
        '<Goal id="G_01" scope="trace" priority="required">Verify the public contract.</Goal>'
        '<Observation id="Obs_01">The scenario battery completed.</Observation>'
        '<Concluding id="Concl_01" for_goal="G_01" status="met">REFER:Obs_01 verified.</Concluding>'
        "</Step>"
    ),
    "goal_concluding_negative": (
        '<Step id="S_01">'
        '<Goal id="G_01" scope="trace" priority="required">Verify the public contract.</Goal>'
        '<Observation id="Obs_01">The scenario battery completed.</Observation>'
        '<Concluding id="Concl_01" for_goal="G_99" status="met">REFER:Obs_01 dangling goal.</Concluding>'
        "</Step>"
    ),
    "action_finding_positive": (
        '<Step id="S_01">'
        '<Action id="Act_01">Wrote the sandbox artifact.</Action>'
        '<Finding id="F_01" status="met">REFER:Act_01 the result is recorded.</Finding>'
        "</Step>"
    ),
    "action_finding_negative": (
        '<Step id="S_01"><Action id="Act_01">Wrote the sandbox artifact.</Action></Step>'
    ),
    "hef_positive": (
        '<Step id="S_01">'
        '<Hypothesis id="H_01">The export round-trips.</Hypothesis>'
        '<Evidence id="E_01" for="H_01" polarity="supports">The exported trace parsed.</Evidence>'
        '<Finding id="F_01" for_hyp="H_01" status="met">REFER:E_01 hypothesis met.</Finding>'
        "</Step>"
    ),
    "hef_negative": (
        '<Step id="S_01">'
        '<Hypothesis id="H_01">The export round-trips.</Hypothesis>'
        '<Evidence id="E_01" for="H_99" polarity="supports">The exported trace parsed.</Evidence>'
        '<Finding id="F_01" for_hyp="H_01" status="met">REFER:E_01 hypothesis met.</Finding>'
        "</Step>"
    ),
    "unknown_kind_negative": '<Step id="S_01"><Bogus id="B_01">not a Scholia atom</Bogus></Step>',
    "dangling_reference_negative": (
        '<Step id="S_01">'
        '<Goal id="G_01" scope="trace" priority="required">Verify references.</Goal>'
        '<Finding id="F_01" status="met">REFER:F_99 cites a missing atom.</Finding>'
        '<Concluding id="Concl_01" for_goal="G_01" status="met">REFER:F_01 done.</Concluding>'
        "</Step>"
    ),
}


def _harden_sys_path() -> None:
    """Drop the CWD from module search so an untrusted working directory can
    never shadow the stdlib the verifier itself runs on."""
    cwd = os.getcwd()
    sys.path[:] = [
        entry
        for entry in sys.path
        if entry not in ("", ".") and os.path.realpath(entry or ".") != os.path.realpath(cwd)
    ]


# ---------------------------------------------------------------------------
# Scenario manifest (story S1): the explicit, bounded conformance matrix.
# ---------------------------------------------------------------------------


def _scenario(scenario_id, title, *, family, scenario_class, boundary, arms, expected, required=True):
    return {
        "id": scenario_id,
        "title": title,
        "family": family,
        "class": scenario_class,
        "boundary": boundary,
        "arms": list(arms),
        "required": required,
        "expected": expected,
        "evidence_file": f"{scenario_id}.json",
    }


MANIFEST = [
    _scenario(
        "catalog_completeness",
        "Catalog advertises the full grammar surface",
        family="catalog",
        scenario_class="positive",
        boundary="mcp:scholia_catalog",
        arms=PLUGIN_ARMS,
        expected=(
            "core atom kinds and all canonical operators present; grammar "
            f"v{STABLE_GRAMMAR_VERSION} and validator/package versions reported; "
            "database path inside the sandbox store"
        ),
    ),
    _scenario(
        "catalog_lookup_negative",
        "Lookup misses honestly and hits real terms",
        family="catalog",
        scenario_class="negative",
        boundary="mcp:scholia_lookup",
        arms=PLUGIN_ARMS,
        expected="a nonsense term returns an error result with zero matches; a real term matches",
    ),
    _scenario(
        "goal_concluding_positive",
        "A goal-closing trace lints clean",
        family="goal_concluding",
        scenario_class="positive",
        boundary="mcp:scholia_lint_trace",
        arms=PLUGIN_ARMS,
        expected="ok with zero errors",
    ),
    _scenario(
        "goal_concluding_negative",
        "A Concluding for a missing Goal is rejected",
        family="goal_concluding",
        scenario_class="negative",
        boundary="mcp:scholia_lint_trace",
        arms=PLUGIN_ARMS,
        expected="not ok; for_goal_resolves fires on the Concluding",
    ),
    _scenario(
        "action_finding_positive",
        "An Action with a recording Finding lints clean",
        family="action_finding",
        scenario_class="positive",
        boundary="mcp:scholia_lint_trace",
        arms=PLUGIN_ARMS,
        expected="ok with zero errors",
    ),
    _scenario(
        "action_finding_negative",
        "A bare Action with no result is rejected",
        family="action_finding",
        scenario_class="negative",
        boundary="mcp:scholia_lint_trace",
        arms=PLUGIN_ARMS,
        expected="not ok; action_recorded fires on the Action",
    ),
    _scenario(
        "hypothesis_evidence_finding_positive",
        "A Hypothesis→Evidence→Finding chain lints clean",
        family="hypothesis_evidence_finding",
        scenario_class="positive",
        boundary="mcp:scholia_lint_trace",
        arms=PLUGIN_ARMS,
        expected="ok with zero errors",
    ),
    _scenario(
        "hypothesis_evidence_finding_negative",
        "Evidence for a missing Hypothesis is rejected",
        family="hypothesis_evidence_finding",
        scenario_class="negative",
        boundary="mcp:scholia_lint_trace",
        arms=PLUGIN_ARMS,
        expected="not ok; reference_complete fires on the Evidence",
    ),
    _scenario(
        "unknown_kind_negative",
        "Unknown atom kinds are rejected at both lint and DAG boundaries",
        family="invalid_input",
        scenario_class="negative",
        boundary="mcp:scholia_lint_trace,scholia_dag_add_atom",
        arms=PLUGIN_ARMS,
        expected="lint reports well_formed; dag_add_atom fails with JSON-RPC -32602",
    ),
    _scenario(
        "dangling_reference_negative",
        "Dangling references are rejected at both lint and DAG boundaries",
        family="invalid_input",
        scenario_class="negative",
        boundary="mcp:scholia_lint_trace,scholia_dag_read",
        arms=PLUGIN_ARMS,
        expected="lint reports reference_complete; reading a missing DAG fails with JSON-RPC -32602",
    ),
    _scenario(
        "dag_lifecycle_roundtrip",
        "DAG create/add/link/export round trip re-validates",
        family="dag_lifecycle",
        scenario_class="composition",
        boundary="mcp:scholia_dag_start,scholia_dag_add_atom,scholia_dag_link,scholia_dag_export,scholia_lint_trace",
        arms=PLUGIN_ARMS,
        expected="JSON export parses with every atom; XML export lints clean under the full grammar",
    ),
    _scenario(
        "session_finish_roundtrip",
        "Session ensure/finish closes the goal and re-validates",
        family="dag_lifecycle",
        scenario_class="composition",
        boundary="mcp:scholia_dag_ensure_session,scholia_dag_finish_session,scholia_dag_export,scholia_lint_trace",
        arms=PLUGIN_ARMS,
        expected="finish appends a status-bearing goal-closing Concluding; the finished trace lints clean",
    ),
    _scenario(
        "search_neighbors_compaction",
        "Search, neighborhood, and compaction smoke",
        family="dag_query",
        scenario_class="composition",
        boundary="mcp:scholia_dag_search,scholia_dag_neighbors,scholia_dag_frontier,scholia_dag_compact",
        arms=PLUGIN_ARMS,
        expected="search hits and misses honestly; neighbors return linked atoms; compaction persists a summary",
    ),
    _scenario(
        "shared_spec_fixtures",
        "Shared scholialang-spec action_recorded corpus agrees",
        family="shared_fixtures",
        scenario_class="composition",
        boundary="mcp:scholia_lint_trace",
        arms=("canonical-plugin", "vendored-codex", "vendored-ollama"),
        expected=(
            "every trace-only corpus case reproduces its declared rule-local outcome; "
            "an absent corpus is reported, never silently passed"
        ),
    ),
    _scenario(
        "installed_wheel_resolved",
        "A clean release wheel is resolved or built from source",
        family="installed_artifact",
        scenario_class="installed",
        boundary="cli:pip wheel",
        arms=(INSTALLED_ARM,),
        expected="a wheel whose version matches the source tree release",
    ),
    _scenario(
        "installed_wheel_clean_install",
        "The wheel installs into an empty target site",
        family="installed_artifact",
        scenario_class="installed",
        boundary="cli:pip install --no-deps --target",
        arms=(INSTALLED_ARM,),
        expected="dist metadata matches the release and declares both console entry points",
    ),
    _scenario(
        "installed_server_protocol",
        "The installed entry point serves MCP end to end",
        family="installed_artifact",
        scenario_class="installed",
        boundary="mcp:python -m scholialang_mcp",
        arms=(INSTALLED_ARM,),
        expected="initialize/tools/list/tools/call succeed against a synthesized sandbox project",
    ),
    _scenario(
        "installed_disabled_mode_refuses",
        "The installed server fails closed when disabled",
        family="installed_artifact",
        scenario_class="negative",
        boundary="mcp:python -m scholialang_mcp (mode=off)",
        arms=(INSTALLED_ARM,),
        expected="tool calls are refused, not silently served",
    ),
]

def derive_verdict(results):
    """Derive the overall verdict from required scenarios only.

    ``pass`` requires every required scenario to pass. Any required failure
    is ``fail``. A required scenario that was skipped, unsupported, or never
    reached makes the run ``incomplete`` — honesty rule: skipped is not pass.
    """
    reasons = []
    required = [r for r in results if r["required"]]
    for row in required:
        if row["status"] != "pass":
            reasons.append(
                {
                    "arm": row["arm"],
                    "scenario_id": row["scenario_id"],
                    "status": row["status"],
                    "message": row["reason"] or row["status"],
                }
            )
    if any(row["status"] == "fail" for row in required):
        verdict = "fail"
    elif reasons:
        verdict = "incomplete"
    else:
        verdict = "pass"
    optional_failures = [
        {"arm": r["arm"], "scenario_id": r["scenario_id"], "status": r["status"], "message": r["reason"] or r["status"]}
        for r in results
        if not r["required"] and r["status"] == "fail"
    ]
    return {
        "verdict": verdict,
        "required_total": len(required),
        "required_passed": sum(1 for r in required if r["status"] == "pass"),
        "reasons": reasons,
        "optional_failures": optional_failures,
    }


# ---------------------------------------------------------------------------
# Evidence: bounded, redacted, deterministic.
# ---------------------------------------------------------------------------


class Normalizer:
    """Rewrite volatile substrings so repeated runs produce identical bytes."""

    def __init__(self):
        self._replacements: list[tuple[str, str]] = []

    def register(self, value, placeholder):
        if value:
            self._replacements.append((str(value), placeholder))

    def apply(self, text: str) -> str:
        for value, placeholder in sorted(self._replacements, key=lambda item: -len(item[0])):
            text = text.replace(value, placeholder)
        text = _DAG_ID_RE.sub("<DAG_ID>", text)
        text = _TIMESTAMP_RE.sub("<TIMESTAMP>", text)
        for digest in set(_PROJECT_KEY_RE.findall(text)):
            text = text.replace(digest, "<PROJECT_KEY>")
        return text


def _secret_values() -> list[str]:
    values = []
    for name, value in os.environ.items():
        if SECRET_ENV_RE.search(name) and value and len(value) >= 6:
            values.append(value)
    return values


def redact(text: str) -> str:
    for value in _secret_values():
        text = text.replace(value, "[redacted]")
    return text


class Recorder:
    """Collect request/response exchanges and named checks for one scenario."""

    def __init__(self, normalizer: Normalizer):
        self._normalizer = normalizer
        self.exchanges = []
        self.checks = []

    def exchange(self, request, response):
        self.exchanges.append({"request": request, "response": response})

    def check(self, name, ok, detail=""):
        self.checks.append({"name": name, "ok": bool(ok), "detail": str(detail)})
        return bool(ok)

    def failed_checks(self):
        return [c for c in self.checks if not c["ok"]]

    def rendered_exchanges(self):
        rendered = []
        for item in self.exchanges:
            text = redact(self._normalizer.apply(json.dumps(item, sort_keys=True)))
            if len(text) > EXCHANGE_CHAR_BUDGET:
                rendered.append(
                    {
                        "truncated": True,
                        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                        "chars": len(text),
                        "head": text[:1200],
                    }
                )
            else:
                rendered.append(json.loads(text))
        return rendered


def write_evidence(evidence_dir: Path, arm: str, scenario: dict, status: str, reason: str, recorder: Recorder, normalizer: Normalizer):
    path = evidence_dir / arm / scenario["evidence_file"]
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "arm": arm,
        "scenario": scenario,
        "status": status,
        "reason": redact(normalizer.apply(reason)),
        "checks": [
            {"name": c["name"], "ok": c["ok"], "detail": redact(normalizer.apply(c["detail"]))}
            for c in recorder.checks
        ],
        "exchanges": recorder.rendered_exchanges(),
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Public-boundary clients.
# ---------------------------------------------------------------------------


class McpClient:
    """Line-delimited JSON-RPC over stdio against a real server subprocess."""

    def __init__(self, argv, env, cwd, recorder: Recorder):
        self._recorder = recorder
        self._next_id = 0
        self._proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            cwd=str(cwd),
        )

    def request(self, method, params):
        self._next_id += 1
        payload = {"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params}
        assert self._proc.stdin is not None and self._proc.stdout is not None
        self._proc.stdin.write(json.dumps(payload) + "\n")
        self._proc.stdin.flush()
        line = self._proc.stdout.readline()
        if not line:
            raise RuntimeError(f"server closed stdout during {method}")
        response = json.loads(line)
        self._recorder.exchange(payload, response)
        return response

    def initialize(self, protocol_version):
        return self.request(
            "initialize",
            {
                "protocolVersion": protocol_version,
                "capabilities": {},
                "clientInfo": {"name": SKILL_NAME, "version": VERIFY_VERSION},
            },
        )

    def tool_names(self):
        listed = self.request("tools/list", {})
        return [tool["name"] for tool in listed["result"]["tools"]]

    def call(self, name, arguments):
        response = self.request("tools/call", {"name": name, "arguments": arguments})
        if "error" in response:
            return {"rpc_error": response["error"]}
        result = response["result"]
        text = result["content"][0]["text"] if result.get("content") else ""
        return {
            "structured": result.get("structuredContent"),
            "text": text,
            "is_error": bool(result.get("isError")),
        }

    def close(self):
        try:
            self._proc.terminate()
            self._proc.wait(timeout=5)
        except Exception:
            self._proc.kill()


def sandbox_env(sandbox: Path, extra=None):
    """A minimal allowlisted environment: nothing from the caller leaks in
    except PATH, and every stateful surface points into the sandbox."""
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(sandbox / "home"),
        "SCHOLIALANG_HOME": str(sandbox / "store"),
        "SCHOLIA_SESSION_ID": "verify-session",
        "SCHOLIA_HOST": "verify",
    }
    env.update(extra or {})
    return env


# ---------------------------------------------------------------------------
# Plugin-arm scenarios.
# ---------------------------------------------------------------------------


def _scenario_sandbox(arm_dir: Path, scenario_id: str) -> Path:
    sandbox = arm_dir / scenario_id
    (sandbox / "home").mkdir(parents=True, exist_ok=True)
    (sandbox / "project").mkdir(parents=True, exist_ok=True)
    return sandbox


def _plugin_client(root: Path, arm: str, sandbox: Path, recorder: Recorder) -> McpClient:
    server = root / PLUGIN_SERVERS[arm]
    if not server.is_file():
        raise FileNotFoundError(f"plugin server not found: {server}")
    client = McpClient(
        [sys.executable, str(server)],
        sandbox_env(sandbox),
        sandbox,
        recorder,
    )
    client.initialize(PLUGIN_PROTOCOL_VERSION)
    return client


def _lint(client: McpClient, snippet: str):
    return client.call("scholia_lint_trace", {"snippet": snippet})


def _lint_rules(result):
    return sorted({error["rule"] for error in result["structured"]["errors"]})


def scenario_catalog_completeness(ctx, recorder):
    client = ctx["client"]
    catalog = client.call("scholia_catalog", {})["structured"]
    kinds = set(catalog.get("scholia_atom_kinds_v05", []))
    recorder.check("core_atom_kinds_present", set(CORE_ATOM_KINDS) <= kinds, f"missing: {sorted(set(CORE_ATOM_KINDS) - kinds)}")
    operators = catalog.get("scholia_canonical_operators_v05", [])
    recorder.check("canonical_operator_count", len(operators) == CANONICAL_OPERATOR_COUNT, f"got {len(operators)}")
    recorder.check(
        "grammar_version",
        catalog.get("language_grammar_version") == STABLE_GRAMMAR_VERSION,
        str(catalog.get("language_grammar_version")),
    )
    recorder.check("validator_version_reported", bool(catalog.get("scholia_validator_version")), "")
    recorder.check("package_version_reported", bool(catalog.get("package_version")), "")
    recorder.check("atom_reference_entries", len(catalog.get("atoms", [])) >= len(CORE_ATOM_KINDS), f"{len(catalog.get('atoms', []))} entries")
    db_path = catalog.get("database_path", "")
    recorder.check(
        "database_path_inside_sandbox",
        db_path.startswith(str(ctx["sandbox"])),
        "store escaped the sandbox" if not db_path.startswith(str(ctx["sandbox"])) else "sandboxed",
    )


def scenario_catalog_lookup_negative(ctx, recorder):
    client = ctx["client"]
    miss = client.call("scholia_lookup", {"term": "zzz-not-a-scholia-term"})
    recorder.check("miss_is_error", miss["is_error"] is True, "")
    recorder.check("miss_has_no_matches", miss["structured"]["matches"] == [], "")
    hit = client.call("scholia_lookup", {"term": "concluding"})
    recorder.check("hit_matches", not hit["is_error"] and len(hit["structured"]["matches"]) >= 1, "")


def _expect_clean(recorder, result, label):
    recorder.check(f"{label}_ok", result["structured"]["ok"] is True, json.dumps(_lint_rules(result)))
    recorder.check(f"{label}_zero_errors", result["structured"]["total_errors"] == 0, "")


def _expect_rules(recorder, result, label, rules, atom_ids=()):
    got = _lint_rules(result)
    recorder.check(f"{label}_not_ok", result["structured"]["ok"] is False, "")
    recorder.check(f"{label}_rules", set(rules) <= set(got), f"expected {sorted(rules)} within {got}")
    flagged = {error["atom_id"] for error in result["structured"]["errors"]}
    for atom_id in atom_ids:
        recorder.check(f"{label}_flags_{atom_id}", atom_id in flagged, f"flagged: {sorted(flagged)}")


def scenario_goal_concluding_positive(ctx, recorder):
    _expect_clean(recorder, _lint(ctx["client"], TRACES["goal_concluding_positive"]), "goal_concluding")


def scenario_goal_concluding_negative(ctx, recorder):
    result = _lint(ctx["client"], TRACES["goal_concluding_negative"])
    _expect_rules(recorder, result, "goal_concluding", {"for_goal_resolves"}, atom_ids=("Concl_01",))


def scenario_action_finding_positive(ctx, recorder):
    _expect_clean(recorder, _lint(ctx["client"], TRACES["action_finding_positive"]), "action_finding")


def scenario_action_finding_negative(ctx, recorder):
    result = _lint(ctx["client"], TRACES["action_finding_negative"])
    _expect_rules(recorder, result, "action_finding", {"action_recorded"}, atom_ids=("Act_01",))


def scenario_hef_positive(ctx, recorder):
    _expect_clean(recorder, _lint(ctx["client"], TRACES["hef_positive"]), "hef")


def scenario_hef_negative(ctx, recorder):
    result = _lint(ctx["client"], TRACES["hef_negative"])
    _expect_rules(recorder, result, "hef", {"reference_complete"}, atom_ids=("E_01",))


def scenario_unknown_kind_negative(ctx, recorder):
    result = _lint(ctx["client"], TRACES["unknown_kind_negative"])
    _expect_rules(recorder, result, "unknown_kind", {"well_formed"})
    dag_error = ctx["client"].call(
        "scholia_dag_add_atom",
        {"dag_id": "dag_irrelevant", "kind": "NotAKind", "summary": "invalid", "project_path": str(ctx["sandbox"] / "project")},
    )
    recorder.check("dag_rejects_unknown_kind", dag_error.get("rpc_error", {}).get("code") == -32602, json.dumps(dag_error.get("rpc_error")))


def scenario_dangling_reference_negative(ctx, recorder):
    result = _lint(ctx["client"], TRACES["dangling_reference_negative"])
    _expect_rules(recorder, result, "dangling_reference", {"reference_complete"}, atom_ids=("F_01",))
    missing = ctx["client"].call(
        "scholia_dag_read",
        {"dag_id": "dag_does_not_exist", "project_path": str(ctx["sandbox"] / "project")},
    )
    recorder.check("dag_read_missing_is_error", missing.get("rpc_error", {}).get("code") == -32602, json.dumps(missing.get("rpc_error")))


def _build_lifecycle_dag(ctx, recorder):
    client = ctx["client"]
    project = str(ctx["sandbox"] / "project")
    started = client.call(
        "scholia_dag_start",
        {"title": "Verify lifecycle", "objective": "Exercise the public DAG lifecycle.", "project_path": project},
    )["structured"]
    dag_id = started["dag_id"]
    goal_id = started["goal_atom"]["id"]
    recorder.check("start_minted_goal", started["goal_atom"]["kind"] == "Goal", goal_id)

    def add(kind, summary, attributes=None, links=None):
        args = {"dag_id": dag_id, "kind": kind, "summary": summary, "content": summary, "project_path": project}
        if attributes:
            args["attributes"] = attributes
        if links:
            args["links"] = links
        return client.call("scholia_dag_add_atom", args)["structured"]["atom"]["id"]

    hypothesis = add("Hypothesis", "The lifecycle round-trips through export and validation.")
    evidence = add(
        "Evidence",
        "The exported trace parsed and validated.",
        attributes={"for": hypothesis, "polarity": "supports"},
        links=[{"to": hypothesis, "relation": "supports"}],
    )
    finding = add(
        "Finding",
        "The hypothesis is met.",
        attributes={"for_hyp": hypothesis, "status": "met"},
        links=[{"to": evidence, "relation": "derived_from"}],
    )
    action = add("Action", "Persisted the sandbox lifecycle artifact.")
    add(
        "Finding",
        "The action result is recorded.",
        attributes={"status": "met"},
        links=[{"to": action, "relation": "records_result"}],
    )
    add(
        "Concluding",
        "The lifecycle goal is met.",
        attributes={"for_goal": goal_id, "status": "met"},
        links=[{"to": finding, "relation": "derived_from"}, {"to": goal_id, "relation": "refers"}],
    )
    linked = client.call(
        "scholia_dag_link",
        {"dag_id": dag_id, "from": finding, "to": hypothesis, "relation": "supports", "project_path": project},
    )["structured"]
    recorder.check("explicit_link_created", linked["edge"]["relation"] == "supports", "")
    return dag_id, {"goal": goal_id, "hypothesis": hypothesis, "evidence": evidence, "finding": finding}


def scenario_dag_lifecycle_roundtrip(ctx, recorder):
    client = ctx["client"]
    project = str(ctx["sandbox"] / "project")
    dag_id, atoms = _build_lifecycle_dag(ctx, recorder)

    exported_json = client.call("scholia_dag_export", {"dag_id": dag_id, "format": "json", "project_path": project})
    parsed = json.loads(exported_json["text"])
    recorder.check("json_export_parses", isinstance(parsed, dict), "")
    recorder.check("json_export_has_all_atoms", len(parsed.get("nodes", {})) == 7, f"{len(parsed.get('nodes', {}))} nodes")
    recorder.check("json_export_has_edges", len(parsed.get("edges", [])) >= 6, f"{len(parsed.get('edges', []))} edges")

    exported_xml = client.call("scholia_dag_export", {"dag_id": dag_id, "format": "xml", "project_path": project})
    linted = _lint(client, exported_xml["text"])
    recorder.check("xml_export_lints_clean", linted["structured"]["ok"] is True, json.dumps(_lint_rules(linted)))

    read = client.call("scholia_dag_read", {"dag_id": dag_id, "project_path": project})["structured"]
    recorder.check("read_returns_graph", "dag" in read and "edges" in read, "")
    summary = client.call("scholia_dag_summary", {"dag_id": dag_id, "project_path": project})["structured"]
    recorder.check("summary_returns_frontier", "frontier" in summary, "")
    ctx["arm_state"]["lifecycle"] = {"dag_id": dag_id, "atoms": atoms, "sandbox": ctx["sandbox"]}


def scenario_session_finish_roundtrip(ctx, recorder):
    client = ctx["client"]
    project = str(ctx["sandbox"] / "project")
    ensured = client.call("scholia_dag_ensure_session", {"project_path": project, "auto": False})["structured"]
    recorder.check("session_created", ensured.get("created") is True, json.dumps({k: ensured.get(k) for k in ("created", "enabled")}))
    again = client.call("scholia_dag_ensure_session", {"project_path": project, "auto": False})["structured"]
    recorder.check("ensure_is_idempotent", again.get("created") is False and again.get("dag_id") == ensured.get("dag_id"), "")
    finished = client.call("scholia_dag_finish_session", {"project_path": project, "summary": "Verification session ended."})["structured"]
    recorder.check("finish_found_session", finished.get("found") is True, "")
    atom = finished.get("atom", {})
    recorder.check("finish_appends_concluding", atom.get("kind") == "Concluding", str(atom.get("kind")))
    attributes = atom.get("attributes", {})
    recorder.check(
        "concluding_closes_goal_with_status",
        bool(attributes.get("for_goal")) and attributes.get("status") in {"met", "unmet", "partially_met"},
        json.dumps(attributes),
    )
    exported = client.call("scholia_dag_export", {"dag_id": finished["dag_id"], "format": "xml", "project_path": project})
    linted = _lint(client, exported["text"])
    recorder.check("finished_session_lints_clean", linted["structured"]["ok"] is True, json.dumps(_lint_rules(linted)))


def scenario_search_neighbors_compaction(ctx, recorder):
    client = ctx["client"]
    project = str(ctx["sandbox"] / "project")
    dag_id, atoms = _build_lifecycle_dag(ctx, recorder)

    hit = client.call("scholia_dag_search", {"dag_id": dag_id, "query": "round-trips", "project_path": project})["structured"]
    recorder.check("search_finds_hypothesis", any(m["atom_id"] == atoms["hypothesis"] for m in hit["matches"]), json.dumps(hit["matches"]))
    miss = client.call("scholia_dag_search", {"dag_id": dag_id, "query": "zzz-absent-content", "project_path": project})["structured"]
    recorder.check("search_misses_honestly", miss["matches"] == [], "")

    neighbors = client.call(
        "scholia_dag_neighbors",
        {"dag_id": dag_id, "atom_id": atoms["hypothesis"], "project_path": project},
    )["structured"]
    neighbor_ids = {node["id"] for node in neighbors["nodes"]}
    recorder.check(
        "neighbors_return_linked_atoms",
        {atoms["hypothesis"], atoms["evidence"], atoms["finding"]} <= neighbor_ids,
        json.dumps(sorted(neighbor_ids)),
    )
    frontier = client.call("scholia_dag_frontier", {"dag_id": dag_id, "project_path": project})["structured"]
    recorder.check("frontier_responds", isinstance(frontier, dict), "")

    compacted = client.call("scholia_dag_compact", {"dag_id": dag_id, "project_path": project})["structured"]
    recorder.check("compaction_returns_summary", bool(compacted.get("summary")), "")
    recompacted = client.call("scholia_dag_compact", {"dag_id": dag_id, "project_path": project})["structured"]
    recorder.check("compaction_is_repeatable", recompacted.get("summary") == compacted.get("summary"), "")


def scenario_shared_spec_fixtures(ctx, recorder):
    corpus = ctx["fixtures_dir"] / SPEC_FIXTURE_FILE
    if not corpus.is_file():
        return ("not_run", f"shared scholialang-spec corpus unavailable at {corpus}")
    suite = json.loads(corpus.read_text(encoding="utf-8"))
    recorder.check("corpus_targets_stable_grammar", suite.get("spec_version") == STABLE_GRAMMAR_VERSION, str(suite.get("spec_version")))
    driven = 0
    adapted_out = 0
    for case in suite["cases"]:
        if case.get("graph_edges"):
            # The lint boundary carries the trace text only; edge-form
            # acceptance cases need the graph view and are reported, not
            # silently counted as covered.
            adapted_out += 1
            continue
        result = _lint(ctx["client"], case["trace"])
        counts = result["structured"]["counts_by_rule"].get(suite["rule"], 0)
        expects = case["expects"]
        ok = counts == expects["error_count"] and (expects["outcome"] == "pass") == (counts == 0)
        recorder.check(f"case_{case['id']}", ok, f"rule errors={counts}, expected {expects['error_count']}")
        driven += 1
    recorder.check("cases_driven", driven > 0, f"{driven} driven, {adapted_out} graph-edge cases outside the lint boundary")
    return None


PLUGIN_SCENARIOS = {
    "catalog_completeness": scenario_catalog_completeness,
    "catalog_lookup_negative": scenario_catalog_lookup_negative,
    "goal_concluding_positive": scenario_goal_concluding_positive,
    "goal_concluding_negative": scenario_goal_concluding_negative,
    "action_finding_positive": scenario_action_finding_positive,
    "action_finding_negative": scenario_action_finding_negative,
    "hypothesis_evidence_finding_positive": scenario_hef_positive,
    "hypothesis_evidence_finding_negative": scenario_hef_negative,
    "unknown_kind_negative": scenario_unknown_kind_negative,
    "dangling_reference_negative": scenario_dangling_reference_negative,
    "dag_lifecycle_roundtrip": scenario_dag_lifecycle_roundtrip,
    "session_finish_roundtrip": scenario_session_finish_roundtrip,
    "search_neighbors_compaction": scenario_search_neighbors_compaction,
    "shared_spec_fixtures": scenario_shared_spec_fixtures,
}


# ---------------------------------------------------------------------------
# Installed-artifact arm.
# ---------------------------------------------------------------------------


def _source_version(root: Path):
    try:
        project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8")).get("project", {})
    except (OSError, tomllib.TOMLDecodeError):
        return None
    if project.get("name") != MCP_PACKAGE_DIST:
        return None
    return project.get("version")


def _run_pip(args, recorder, label):
    completed = subprocess.run(
        [sys.executable, "-m", "pip", *args],
        capture_output=True,
        text=True,
        timeout=PIP_TIMEOUT_S,
        env={**os.environ, "PIP_DISABLE_PIP_VERSION_CHECK": "1"},
    )
    detail = "" if completed.returncode == 0 else (completed.stderr or completed.stdout)[-800:]
    recorder.check(label, completed.returncode == 0, detail)
    return completed.returncode == 0


def scenario_installed_wheel_resolved(ctx, recorder):
    expected = _source_version(ctx["root"])
    if ctx["wheel"] is not None:
        wheel = ctx["wheel"]
        if not wheel.is_file():
            recorder.check("wheel_fixture_exists", False, str(wheel))
            return ("fail", "the provided wheel fixture does not exist")
        recorder.check("wheel_fixture_exists", True, "")
    else:
        if expected is None:
            return ("unsupported", "no wheel provided and the root is not a scholialang-mcp source tree")
        if not _run_pip(
            ["wheel", "--no-deps", "-w", str(ctx["sandbox"]), str(ctx["root"])],
            recorder,
            "wheel_build_succeeds",
        ):
            return ("fail", "pip wheel failed for the source tree")
        wheels = sorted(ctx["sandbox"].glob("scholialang_mcp-*.whl"))
        if not recorder.check("wheel_artifact_produced", bool(wheels), ""):
            return ("fail", "pip wheel produced no scholialang_mcp wheel")
        wheel = wheels[0]
    version = wheel.name.split("-")[1] if "-" in wheel.name else None
    if expected is not None:
        recorder.check("wheel_version_matches_source", version == expected, f"wheel {version} vs source {expected}")
    ctx["arm_state"]["wheel"] = wheel
    ctx["arm_state"]["wheel_version"] = version
    return None


def scenario_installed_wheel_clean_install(ctx, recorder):
    wheel = ctx["arm_state"].get("wheel")
    if wheel is None:
        return ("not_run", "no wheel was resolved by the previous scenario")
    site = ctx["sandbox"] / "site"
    site.mkdir(parents=True, exist_ok=True)
    if not _run_pip(
        ["install", "--no-deps", "--quiet", "--target", str(site), str(wheel)],
        recorder,
        "clean_target_install_succeeds",
    ):
        return ("fail", "pip install --no-deps --target failed for the wheel")
    dist_infos = sorted(site.glob("scholialang_mcp-*.dist-info"))
    if not recorder.check("dist_info_present", bool(dist_infos), ""):
        return ("fail", "no dist-info directory in the target site")
    metadata = (dist_infos[0] / "METADATA").read_text(encoding="utf-8")
    version_match = re.search(r"^Version: (.+)$", metadata, re.MULTILINE)
    installed_version = version_match.group(1) if version_match else None
    recorder.check(
        "installed_version_matches_wheel",
        installed_version == ctx["arm_state"].get("wheel_version"),
        f"installed {installed_version}",
    )
    entry_points = (dist_infos[0] / "entry_points.txt").read_text(encoding="utf-8")
    for script in ("scholialang-mcp", "scholialang-lsp"):
        recorder.check(f"entry_point_{script}", f"{script} = " in entry_points, "")
    ctx["arm_state"]["site"] = site
    return None


def _synthesize_atlas_project(sandbox: Path) -> Path:
    """A minimal, deterministic project the installed server can serve."""
    repo = sandbox / "sample-project"
    (repo / "src").mkdir(parents=True, exist_ok=True)
    files = repo / ".scholia-atlas" / "files"
    files.mkdir(parents=True, exist_ok=True)
    (repo / "src" / "sample.py").write_text('def thing() -> str:\n    return "sample"\n', encoding="utf-8")
    (repo / ".scholia-atlas" / "tree.json").write_text(
        json.dumps({"name": "sample", "children": [{"path": "src/sample.py", "summary": "Small sample module."}]}),
        encoding="utf-8",
    )
    (files / "src%2Fsample.py.json").write_text(
        json.dumps(
            {
                "source_path": "src/sample.py",
                "granularity": "file",
                "prose_preamble": "Small sample module exposing thing().",
                "scholia_codeblock": '<Observation id="Obs_01">thing returns a sample string.</Observation>',
                "metadata": {"spec_version": "0.4.0"},
            }
        ),
        encoding="utf-8",
    )
    return repo


def _installed_client(ctx, recorder, extra_env=None) -> McpClient:
    site = ctx["arm_state"]["site"]
    project = _synthesize_atlas_project(ctx["sandbox"])
    env = sandbox_env(ctx["sandbox"], {"PYTHONPATH": str(site), **(extra_env or {})})
    return McpClient(
        [sys.executable, "-m", "scholialang_mcp", "--repo-root", str(project)],
        env,
        ctx["sandbox"],
        recorder,
    )


def scenario_installed_server_protocol(ctx, recorder):
    if ctx["arm_state"].get("site") is None:
        return ("not_run", "no installed target site from the previous scenario")
    client = _installed_client(ctx, recorder)
    try:
        init = client.initialize(WHEEL_PROTOCOL_VERSION)
        server_info = init["result"]["serverInfo"]
        recorder.check("initialize_negotiates", init["result"]["protocolVersion"] == WHEEL_PROTOCOL_VERSION, "")
        expected_version = ctx["arm_state"].get("wheel_version")
        recorder.check(
            "server_reports_wheel_version",
            server_info.get("version") == expected_version,
            f"server {server_info.get('version')} vs wheel {expected_version}",
        )
        tools = client.tool_names()
        recorder.check("tool_count", len(tools) == WHEEL_SERVER_TOOL_COUNT, f"{len(tools)} tools")
        recorder.check("advertised_tools_present", WHEEL_SERVER_TOOLS <= set(tools), json.dumps(sorted(tools)))
        looked_up = client.call("lookup_file_summary", {"path": "src/sample.py"})
        payload = json.loads(looked_up["text"])
        recorder.check(
            "tool_call_round_trips",
            payload.get("prose_preamble") == "Small sample module exposing thing().",
            "",
        )
    finally:
        client.close()


def scenario_installed_disabled_mode_refuses(ctx, recorder):
    if ctx["arm_state"].get("site") is None:
        return ("not_run", "no installed target site from the previous scenario")
    client = _installed_client(ctx, recorder, {"SCHOLIALANG_MCP_SERVER_MODE": "off"})
    try:
        client.initialize(WHEEL_PROTOCOL_VERSION)
        refused = client.call("lookup_file_summary", {"path": "src/sample.py"})
        payload = json.loads(refused["text"])
        recorder.check("disabled_mode_refuses", payload.get("status") == "refused", json.dumps(payload)[:200])
    finally:
        client.close()


INSTALLED_SCENARIOS = {
    "installed_wheel_resolved": scenario_installed_wheel_resolved,
    "installed_wheel_clean_install": scenario_installed_wheel_clean_install,
    "installed_server_protocol": scenario_installed_server_protocol,
    "installed_disabled_mode_refuses": scenario_installed_disabled_mode_refuses,
}


# ---------------------------------------------------------------------------
# Orchestration.
# ---------------------------------------------------------------------------


def run_scenario(scenario, arm, ctx, normalizer, evidence_dir, skip_ids):
    recorder = Recorder(normalizer)
    ctx = dict(ctx)
    status, reason = "pass", ""
    if scenario["id"] in skip_ids:
        status, reason = "not_run", "skipped by operator request"
    else:
        try:
            if arm in PLUGIN_ARMS:
                sandbox = _scenario_sandbox(ctx["arm_dir"], scenario["id"])
                ctx["sandbox"] = sandbox
                client = _plugin_client(ctx["root"], arm, sandbox, recorder)
                ctx["client"] = client
                try:
                    outcome = PLUGIN_SCENARIOS[scenario["id"]](ctx, recorder)
                finally:
                    client.close()
            else:
                ctx["sandbox"] = ctx["arm_dir"]
                outcome = INSTALLED_SCENARIOS[scenario["id"]](ctx, recorder)
            if outcome is not None:
                status, reason = outcome
            elif recorder.failed_checks():
                failed = ", ".join(c["name"] for c in recorder.failed_checks())
                status, reason = "fail", f"checks failed: {failed}"
        except FileNotFoundError as exc:
            status, reason = "unsupported", str(exc)
        except Exception as exc:  # honest failure, never a silent skip
            status, reason = "fail", f"{type(exc).__name__}: {exc}"
    evidence_path = write_evidence(evidence_dir, arm, scenario, status, reason, recorder, normalizer)
    return {
        "arm": arm,
        "scenario_id": scenario["id"],
        "family": scenario["family"],
        "class": scenario["class"],
        "required": scenario["required"],
        "status": status,
        "reason": redact(normalizer.apply(reason)),
        "checks_total": len(recorder.checks),
        "checks_passed": sum(1 for c in recorder.checks if c["ok"]),
        "evidence_path": normalizer.apply(str(evidence_path)),
    }


def build_report(*, root, evidence_dir, arms=DEFAULT_ARMS, wheel=None, fixtures_dir=None, skip_ids=(), keep_sandboxes=False):
    root = Path(root).resolve()
    evidence_dir = Path(evidence_dir).resolve()
    evidence_dir.mkdir(parents=True, exist_ok=True)
    fixtures_dir = Path(fixtures_dir).resolve() if fixtures_dir else root / SPEC_FIXTURE_SUBDIR

    normalizer = Normalizer()
    normalizer.register(str(evidence_dir), "<EVIDENCE>")
    normalizer.register(str(root), "<ROOT>")
    if wheel:
        normalizer.register(str(Path(wheel).resolve()), "<WHEEL>")
    normalizer.register(str(fixtures_dir), "<FIXTURES>")

    results = []
    skip_ids = set(skip_ids)
    for arm in arms:
        arm_dir = evidence_dir / "sandboxes" / arm
        arm_dir.mkdir(parents=True, exist_ok=True)
        normalizer.register(str(arm_dir), f"<SANDBOX:{arm}>")
        ctx = {
            "root": root,
            "arm_dir": arm_dir,
            "arm_state": {},
            "wheel": Path(wheel).resolve() if wheel else None,
            "fixtures_dir": fixtures_dir,
        }
        for scenario in MANIFEST:
            if arm not in scenario["arms"]:
                continue
            results.append(run_scenario(scenario, arm, ctx, normalizer, evidence_dir, skip_ids))

    if not keep_sandboxes:
        # Leave only evidence behind: the throwaway stores, homes, sites, and
        # synthesized projects have served their purpose.
        shutil.rmtree(evidence_dir / "sandboxes", ignore_errors=True)

    overall = derive_verdict(results)
    report = {
        "verifier": {
            "name": SKILL_NAME,
            "version": VERIFY_VERSION,
            "schema_version": SCHEMA_VERSION,
            "grammar_version": STABLE_GRAMMAR_VERSION,
            "isolated": True,
        },
        "arms": list(arms),
        "manifest_size": len(MANIFEST),
        "results": results,
        "overall": overall,
    }
    (evidence_dir / "verify_report.json").write_text(
        redact(json.dumps(report, indent=2, sort_keys=True)) + "\n", encoding="utf-8"
    )
    return report


def _render_human(report):
    lines = [f"{SKILL_NAME} {VERIFY_VERSION} (grammar v{STABLE_GRAMMAR_VERSION}, isolated sandboxes)"]
    for row in report["results"]:
        marker = {"pass": "ok", "fail": "FAIL", "unsupported": "n/a", "not_run": "SKIP"}[row["status"]]
        required = "required" if row["required"] else "optional"
        lines.append(
            f"  {row['arm']:<18} {row['scenario_id']:<40} {marker:<5} "
            f"({row['class']}, {required}, {row['checks_passed']}/{row['checks_total']} checks)"
        )
        if row["reason"]:
            lines.append(f"      reason: {row['reason']}")
    overall = report["overall"]
    for reason in overall["reasons"]:
        lines.append(f"  ! {reason['arm']}/{reason['scenario_id']}: [{reason['status']}] {reason['message']}")
    lines.append(
        f"overall: {overall['verdict']} "
        f"({overall['required_passed']}/{overall['required_total']} required scenarios passed)"
    )
    return "\n".join(lines)


def main(argv=None) -> int:
    _harden_sys_path()
    parser = argparse.ArgumentParser(
        prog=SKILL_NAME,
        description="Isolated end-to-end verifier for the public Scholialang contract.",
    )
    parser.add_argument("--root", type=Path, default=None, help="scholialang-mcp checkout root (default: CWD)")
    parser.add_argument(
        "--evidence-dir",
        type=Path,
        required=True,
        help="directory for per-scenario evidence, sandboxes, and verify_report.json",
    )
    parser.add_argument(
        "--arm",
        action="append",
        choices=ALL_ARMS,
        default=None,
        help=f"arms to verify (repeatable; default: {', '.join(DEFAULT_ARMS)})",
    )
    parser.add_argument("--wheel", type=Path, default=None, help="prebuilt wheel fixture for the installed arm")
    parser.add_argument("--fixtures", type=Path, default=None, help="shared scholialang-spec conformance corpus directory")
    parser.add_argument("--skip", action="append", default=None, help="scenario id to mark not_run (repeatable; a skipped required scenario is never a pass)")
    parser.add_argument("--keep-sandboxes", action="store_true", help="keep the throwaway sandbox directories for inspection")
    parser.add_argument("--list", action="store_true", help="print the scenario manifest and exit")
    parser.add_argument("--json", action="store_true", help="emit the stable JSON report")
    args = parser.parse_args(argv)

    if args.list:
        print(json.dumps({"schema_version": SCHEMA_VERSION, "scenarios": MANIFEST}, indent=2, sort_keys=True))
        return 0

    report = build_report(
        root=args.root or Path.cwd(),
        evidence_dir=args.evidence_dir,
        arms=tuple(args.arm) if args.arm else DEFAULT_ARMS,
        wheel=args.wheel,
        fixtures_dir=args.fixtures,
        skip_ids=tuple(args.skip or ()),
        keep_sandboxes=args.keep_sandboxes,
    )
    if args.json:
        print(redact(json.dumps(report, indent=2, sort_keys=True)))
    else:
        print(redact(_render_human(report)))
    return {"pass": 0, "incomplete": 1, "fail": 2}[report["overall"]["verdict"]]


if __name__ == "__main__":
    raise SystemExit(main())
