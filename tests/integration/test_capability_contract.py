"""Frozen capability/identity/durable-state contract fixtures (sch073-04).

Every scenario here is driven by the portable fixture file
``contracts/fixtures/capability-matrix.v1.json`` against a *live* server
process, mirroring ``test_mcp_support_matrix.py``:

* eight supported/unsupported combinations of the three independent
  facets (synthetic adapters — never the shipped default),
* all 27 off/observe/enforce policy combinations,
* incompatible-protocol and per-extension rollout-mode arms,
* foreign-project / forged-identity denial without an existence oracle,
* malformed payloads, the old-consumer legacy arm, and
* identical declared capability contracts across the wheel server, all
  three plugin servers, and the committed contract manifest.

The synthetic adapters activate only through explicit test-arm
environment variables; the shipped-default arms below prove that without
them (and whatever the policies say) discovery advertises nothing.
"""
from __future__ import annotations

import itertools
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from scholialang_mcp import capabilities  # noqa: E402

FIXTURE_REPO = ROOT / "tests" / "fixtures" / "atlas" / "sample"
MATRIX = json.loads(
    (ROOT / "contracts" / "fixtures" / "capability-matrix.v1.json").read_text(encoding="utf-8")
)
MANIFEST = json.loads(
    (ROOT / "contracts" / "mcp-capability-contract.v1.json").read_text(encoding="utf-8")
)
PLUGIN_SERVERS = {
    host: ROOT / "plugins" / host / "scholialang" / "scripts" / "scholialang_mcp_server.py"
    for host in ("claude-code", "codex", "ollama")
}

META_PROTOCOL_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CLIENT_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
MODERN_VERSION = "2026-07-28"
TASKS_KEY = capabilities.TASKS_CAPABILITY_KEY
HEARTBEAT_KEY = capabilities.HEARTBEAT_CAPABILITY_KEY

SEED = [
    {"facet": "tasks", "principal": "alice", "project": "proj-a", "id": "task-a", "record": {"status": "working"}},
    {"facet": "tasks", "principal": "bob", "project": "proj-b", "id": "task-b", "record": {"status": "working"}},
]

FACET_PROBES = {
    "events": {"method": "subscriptions/listen", "params": {"notifications": ["toolsListChanged"]}},
    "tasks": {"method": "tasks/get", "params": {"taskId": "task-a"}},
    "heartbeat": {"method": "com.dougfirlabs/heartbeat.lease", "params": {}},
}


def _modern_meta(version: str = MODERN_VERSION) -> dict[str, Any]:
    return {META_PROTOCOL_VERSION: version, META_CLIENT_CAPABILITIES: {}}


class _Server:
    def __init__(self, cmd: list[str], env_extra: dict[str, str]):
        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT / "src")
        for facet_var in capabilities.POLICY_ENV.values():
            env.pop(facet_var, None)
        for var in (
            capabilities.ENV_SYNTHETIC_FACETS,
            capabilities.ENV_SYNTHETIC_SEED,
            capabilities.ENV_PRINCIPAL,
            capabilities.ENV_PROJECT,
        ):
            env.pop(var, None)
        env.update(env_extra)
        self.proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        self._next_id = 0

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        assert self.proc.stdin is not None and self.proc.stdout is not None
        self._next_id += 1
        payload = {"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params}
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        assert line, "server closed stdout mid-probe"
        return json.loads(line)

    def close(self) -> None:
        self.proc.terminate()


def _wheel_server(env_extra: dict[str, str]) -> _Server:
    return _Server(
        [sys.executable, "-m", "scholialang_mcp", "--repo-root", str(FIXTURE_REPO)],
        env_extra,
    )


def _plugin_server(host: str, env_extra: dict[str, str]) -> _Server:
    return _Server([sys.executable, str(PLUGIN_SERVERS[host])], env_extra)


def _synthetic_env(
    facets: list[str],
    policies: dict[str, str],
    *,
    principal: str = "alice",
    project: str = "proj-a",
) -> dict[str, str]:
    env = {
        capabilities.ENV_SYNTHETIC_FACETS: ",".join(facets),
        capabilities.ENV_SYNTHETIC_SEED: json.dumps(SEED),
        capabilities.ENV_PRINCIPAL: principal,
        capabilities.ENV_PROJECT: project,
    }
    for facet, policy in policies.items():
        env[capabilities.POLICY_ENV[facet]] = policy
    return env


def _discover(server: _Server) -> dict[str, Any]:
    response = server.request("server/discover", {"_meta": _modern_meta()})
    assert "result" in response, response
    return response["result"]


def _advertised_flags(result: dict[str, Any]) -> dict[str, bool]:
    caps = result["capabilities"]
    extensions = caps.get("extensions", {})
    return {
        "capabilities.subscriptions": "subscriptions" in caps,
        f"capabilities.extensions['{TASKS_KEY}']": TASKS_KEY in extensions,
        f"capabilities.extensions['{HEARTBEAT_KEY}']": HEARTBEAT_KEY in extensions,
    }


# ── acceptance 1+2: eight support combinations, independence, fallback ──


@pytest.mark.parametrize("combo", MATRIX["support_matrix"], ids=lambda c: c["id"])
def test_support_combination(combo: dict[str, Any]) -> None:
    facets = [f for f in capabilities.FACETS if combo[f]]
    env = _synthetic_env(facets, {f: "enforce" for f in capabilities.FACETS})
    server = _wheel_server(env)
    try:
        result = _discover(server)
        assert _advertised_flags(result) == combo["expect_advertised"], combo["id"]
        # Unsupported facets refuse with the explicit fallback shape —
        # never a false success — while supported peers keep working.
        for facet in combo["expect_refused_facets"]:
            probe = FACET_PROBES[facet]
            response = server.request(probe["method"], {"_meta": _modern_meta(), **probe["params"]})
            error = response["error"]
            assert error["code"] == MATRIX["refusal_shape"]["error_code"]
            assert error["data"]["facet"] == facet
            assert error["data"]["reason"] == MATRIX["refusal_shape"]["reason"]
            assert sorted(error["data"]) == sorted(MATRIX["refusal_shape"]["data_keys"])
        for facet in facets:
            probe = FACET_PROBES[facet]
            response = server.request(probe["method"], {"_meta": _modern_meta(), **probe["params"]})
            assert "result" in response, (combo["id"], facet, response)
            assert response["result"]["synthetic"] is True
    finally:
        server.close()


# ── acceptance 1: 27 independent off/observe/enforce rollout modes ──


@pytest.mark.parametrize("combo", MATRIX["mode_matrix"], ids=lambda c: c["id"])
def test_policy_mode_combination(combo: dict[str, Any]) -> None:
    env = _synthetic_env(list(capabilities.FACETS), dict(combo["policies"]))
    server = _wheel_server(env)
    try:
        result = _discover(server)
        flags = _advertised_flags(result)
        for facet in capabilities.FACETS:
            key = MANIFEST["declaration"]["facets"][facet]["advertisement"]
            assert flags[key] == (facet in combo["expect_active"]), (combo["id"], facet)
        for facet in capabilities.FACETS:
            if facet in combo["expect_active"]:
                continue
            policy = combo["policies"][facet]
            probe = FACET_PROBES[facet]
            response = server.request(probe["method"], {"_meta": _modern_meta(), **probe["params"]})
            error = response["error"]
            assert error["code"] == -32601, (combo["id"], facet)
            assert error["data"]["policy"] == policy
            # Observe records but can never execute or certify support.
            assert error["data"]["observed"] == (policy == "observe")
    finally:
        server.close()


def test_policy_downgrade_cannot_certify_support() -> None:
    """enforce without an implemented adapter advertises nothing (shipped
    default), and an invalid policy value fails closed to off."""
    server = _wheel_server({capabilities.POLICY_ENV["tasks"]: "enforce"})
    try:
        assert _advertised_flags(_discover(server)) == {
            "capabilities.subscriptions": False,
            f"capabilities.extensions['{TASKS_KEY}']": False,
            f"capabilities.extensions['{HEARTBEAT_KEY}']": False,
        }
    finally:
        server.close()
    env = _synthetic_env(["tasks"], {})
    env[capabilities.POLICY_ENV["tasks"]] = "definitely-not-a-policy"
    server = _wheel_server(env)
    try:
        result = _discover(server)
        assert _advertised_flags(result)[f"capabilities.extensions['{TASKS_KEY}']"] is False
        response = server.request("tasks/get", {"_meta": _modern_meta(), "taskId": "task-a"})
        assert response["error"]["data"]["policy"] == "off"
    finally:
        server.close()


def test_incompatible_protocol_rejected_before_facet_routing() -> None:
    env = _synthetic_env(list(capabilities.FACETS), {f: "enforce" for f in capabilities.FACETS})
    server = _wheel_server(env)
    try:
        arm = MATRIX["incompatible_protocol"]
        response = server.request(
            "tasks/get", {"_meta": _modern_meta(arm["requested"]), "taskId": "task-a"}
        )
        assert response["error"]["code"] == arm["expect_error_code"]
        assert "facet" not in (response["error"].get("data") or {})
    finally:
        server.close()


# ── acceptance 3: identity — foreign project/tenant denial ──


def test_foreign_project_cannot_discover_or_mutate() -> None:
    env = _synthetic_env(["tasks"], {"tasks": "enforce"}, principal="alice", project="proj-a")
    server = _wheel_server(env)
    try:
        own = server.request("tasks/get", {"_meta": _modern_meta(), "taskId": "task-a"})
        assert own["result"]["task"]["status"] == "working"
        foreign_errors = []
        for params in (
            {"taskId": "task-b"},
            # Forged caller-supplied identity must change nothing.
            {"taskId": "task-b", "owner": "bob", "project": "proj-b", "principal": "bob"},
            {"taskId": "no-such-task"},
        ):
            response = server.request("tasks/get", {"_meta": _modern_meta(), **params})
            foreign_errors.append(response["error"])
        for mutate in ("tasks/update", "tasks/cancel"):
            params = {"taskId": "task-b"}
            if mutate == "tasks/update":
                params["inputResponses"] = {}
            response = server.request(mutate, {"_meta": _modern_meta(), **params})
            foreign_errors.append(response["error"])
        for error in foreign_errors:
            assert error["code"] == capabilities.SCOPED_NOT_FOUND
        # Foreign and genuinely-missing lookups are byte-identical: no
        # cross-tenant existence oracle.
        assert foreign_errors[0] == foreign_errors[1] == foreign_errors[2]
    finally:
        server.close()


def test_unbound_principal_fails_closed() -> None:
    env = _synthetic_env(["tasks"], {"tasks": "enforce"})
    del env[capabilities.ENV_PRINCIPAL]
    server = _wheel_server(env)
    try:
        response = server.request("tasks/get", {"_meta": _modern_meta(), "taskId": "task-a"})
        assert response["error"]["code"] == capabilities.PRINCIPAL_UNBOUND
    finally:
        server.close()


def test_store_scope_enforcement_unit() -> None:
    store = capabilities.ScopedMemoryStore(max_records=2)
    alice = capabilities.PrincipalContext("alice", "proj-a")
    bob = capabilities.PrincipalContext("bob", "proj-b")
    revision = store.put("tasks", alice, "t1", {"status": "working"}, notify={"kind": "created"})
    assert revision == 1
    # Notification intent is surfaced only at the commit point, carrying
    # the committed scope revision.
    assert store.committed_notifications[-1]["scope_revision"] == 1
    with pytest.raises(capabilities.ScopeDenied):
        store.get("tasks", bob, "t1")
    assert store.list_ids("tasks", bob) == []
    store.put("tasks", alice, "t2", {})
    with pytest.raises(capabilities.ExtensionMethodError):
        store.put("tasks", alice, "t3", {})  # bounded retention per scope


# ── acceptance 2 (old consumers) and malformed payloads ──


def test_old_consumer_legacy_arm_sees_no_extension_capability() -> None:
    env = _synthetic_env(list(capabilities.FACETS), {f: "enforce" for f in capabilities.FACETS})
    server = _wheel_server(env)
    try:
        init = server.request(
            "initialize", {"protocolVersion": "2025-11-25", "capabilities": {}}
        )
        caps = init["result"]["capabilities"]
        assert "extensions" not in caps and "subscriptions" not in caps
        legacy_call = server.request("tasks/get", {"taskId": "task-a"})
        assert legacy_call["error"]["code"] == -32601
        assert legacy_call["error"]["data"]["reason"] == "modern_protocol_required"
        # The same process still advertises to a modern peer: support is
        # negotiated per request era, never inherited.
        assert _advertised_flags(_discover(server))[
            f"capabilities.extensions['{TASKS_KEY}']"
        ] is True
    finally:
        server.close()


def test_malformed_extension_payloads() -> None:
    env = _synthetic_env(list(capabilities.FACETS), {f: "enforce" for f in capabilities.FACETS})
    server = _wheel_server(env)
    try:
        for method, params in (
            ("tasks/get", {"taskId": 7}),
            ("tasks/get", {}),
            ("tasks/update", {"taskId": "task-a", "inputResponses": "not-an-object"}),
            ("subscriptions/listen", {"notifications": "not-an-array"}),
        ):
            response = server.request(method, {"_meta": _modern_meta(), **params})
            assert response["error"]["code"] == -32602, (method, response)
    finally:
        server.close()


# ── acceptance 4+5: identical declared contracts and exact pins ──


def _declared_contract(server: _Server) -> dict[str, Any]:
    meta = _discover(server)["_meta"]
    return meta[capabilities.META_CAPABILITY_CONTRACT]["declaration"]


def test_declared_contract_identical_across_all_surfaces() -> None:
    declarations = {}
    server = _wheel_server({})
    try:
        declarations["wheel"] = _declared_contract(server)
    finally:
        server.close()
    for host in PLUGIN_SERVERS:
        server = _plugin_server(host, {})
        try:
            declarations[host] = _declared_contract(server)
        finally:
            server.close()
    expected = MANIFEST["declaration"]
    assert expected == capabilities.CAPABILITY_DECLARATION
    for surface, declared in declarations.items():
        assert declared == expected, f"declaration drift on {surface}"


def test_shipped_surfaces_never_advertise_extensions_even_enforced() -> None:
    env = {var: "enforce" for var in capabilities.POLICY_ENV.values()}
    server = _wheel_server(env)
    try:
        caps = _discover(server)["capabilities"]
        assert "extensions" not in caps and "subscriptions" not in caps
    finally:
        server.close()
    for host in PLUGIN_SERVERS:
        server = _plugin_server(host, env)
        try:
            caps = _discover(server)["capabilities"]
            assert "extensions" not in caps and "subscriptions" not in caps, host
        finally:
            server.close()


def test_manifest_records_exact_pins_and_blocking_gaps() -> None:
    pins = MANIFEST["protocol_pins"]
    assert pins["core"].endswith("@e76e9c572c6f2bfcb730357101acc90f2f802e02")
    assert pins["tasks"].endswith("@9263312d11a682ac83f83fe84794d4627efd22f5")
    assert pins["sdk"].endswith("@0921d94a74db900dccd2d534842aa7b6160542d2")
    receipt = MANIFEST["accepted_core_receipt"]
    assert receipt["wheel_sha256"] == "bbe08813bb0431824fa82db6b086ff2aafca5f6024e0b377dcfa7d37c25c1831"
    assert receipt["sdist_sha256"] == "457fe675175adf2c3166eeb55ffe86f8e9e0fb72b5acea54615ac3401c2557b2"
    assert receipt["status"] == "receipt_pinned_not_yet_vendored"
    # Unresolved wire-contract choices must block downstream acceptance.
    gaps = {gap["id"]: gap for gap in MANIFEST["feature_gaps"]}
    for required in ("gap-core-073-vendoring", "gap-durable-store", "gap-real-adapters"):
        assert gaps[required]["blocking"] is True
    declaration = MANIFEST["declaration"]
    for facet in capabilities.FACETS:
        assert declaration["facets"][facet]["implemented"] is False
        assert declaration["facets"][facet]["conformance_certified"] is False
        assert declaration["facets"][facet]["negotiation"] == "independent"
    assert declaration["policy"]["default"] == "off"


def test_fixture_matrix_is_complete() -> None:
    support_ids = {c["id"] for c in MATRIX["support_matrix"]}
    assert len(support_ids) == 8
    expected = {
        f"support/events-{e}/tasks-{t}/heartbeat-{h}"
        for e, t, h in itertools.product((0, 1), repeat=3)
    }
    assert support_ids == expected
    mode_ids = {c["id"] for c in MATRIX["mode_matrix"]}
    assert len(mode_ids) == 27
    expected_modes = {
        f"mode/events-{a}/tasks-{b}/heartbeat-{c}"
        for a, b, c in itertools.product(("off", "observe", "enforce"), repeat=3)
    }
    assert mode_ids == expected_modes
