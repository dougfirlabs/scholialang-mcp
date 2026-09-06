#!/usr/bin/env python3
"""Shared capability-contract conformance runner + manifest validator (sch073-04).

Machine-runnable, stdlib-only, and portable: the separately owned
host consumer can invoke it against this repository (or a clean
clone) to re-verify the frozen capability/identity contract without
pytest. It probes REAL stdio server processes — never in-process mocks —
and validates the committed manifest and fixture matrix.

Usage (from the repository root, any neutral CWD works):

    python3 scripts/capability_conformance.py [--repo-root .] [--json report.json]

Arms executed:

  manifest   contracts/mcp-capability-contract.v1.json: declaration equals
             src constant, exact pins present, feature gaps blocking,
             fixture matrix complete (8 support + 27 mode combinations).
  shipped    wheel + all three plugin servers, all policies forced to
             ``enforce``: discovery must advertise NO extension facet and
             every facet wire method must refuse with explicit fallback.
  parity     the declared contract is identical across all four surfaces.
  legacy     old-consumer initialize arm sees no extension capability and
             cannot reach a facet method.
  protocol   an incompatible protocol version is rejected (-32022) before
             facet routing.
  synthetic  wheel server with synthetic adapters (explicit test arm —
             proves negotiation, NOT shipped transport support): the
             8-combination support matrix and 27 rollout-mode combinations
             from contracts/fixtures/capability-matrix.v1.json.

Exit status 0 iff every check passes; the JSON report lists each check.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

META_PROTOCOL_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CLIENT_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
META_CAPABILITY_CONTRACT = "com.dougfirlabs/scholialang.mcp-capabilities.v1"
MODERN_VERSION = "2026-07-28"
TASKS_KEY = "io.modelcontextprotocol/tasks"
HEARTBEAT_KEY = "com.dougfirlabs/heartbeat"
FACETS = ("events", "tasks", "heartbeat")
POLICY_ENV = {
    "events": "SCHOLIALANG_MCP_EXT_EVENTS_POLICY",
    "tasks": "SCHOLIALANG_MCP_EXT_TASKS_POLICY",
    "heartbeat": "SCHOLIALANG_MCP_EXT_HEARTBEAT_POLICY",
}
FACET_PROBES = {
    "events": ("subscriptions/listen", {"notifications": ["toolsListChanged"]}),
    "tasks": ("tasks/get", {"taskId": "task-a"}),
    "heartbeat": ("com.dougfirlabs/heartbeat.lease", {}),
}
SEED = [
    {"facet": "tasks", "principal": "alice", "project": "proj-a", "id": "task-a", "record": {"status": "working"}},
]


class Report:
    def __init__(self) -> None:
        self.checks: list[dict] = []

    def add(self, arm: str, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append({"arm": arm, "check": name, "ok": bool(ok), "detail": detail})
        if not ok:
            print(f"FAIL [{arm}] {name}: {detail}", file=sys.stderr)

    @property
    def ok(self) -> bool:
        return all(check["ok"] for check in self.checks)


class Server:
    def __init__(self, cmd: list[str], env_extra: dict[str, str], src_path: Path):
        env = os.environ.copy()
        env["PYTHONPATH"] = str(src_path)
        for var in list(POLICY_ENV.values()) + [
            "SCHOLIALANG_MCP_SYNTHETIC_FACETS",
            "SCHOLIALANG_MCP_SYNTHETIC_SEED",
            "SCHOLIALANG_MCP_PRINCIPAL",
            "SCHOLIALANG_MCP_PROJECT",
        ]:
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

    def __enter__(self) -> "Server":
        return self

    def __exit__(self, *exc_info) -> None:
        self.proc.terminate()

    def request(self, method: str, params: dict) -> dict:
        self._next_id += 1
        payload = {"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params}
        assert self.proc.stdin and self.proc.stdout
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()
        line = self.proc.stdout.readline()
        if not line:
            raise RuntimeError(f"server closed stdout during {method}")
        return json.loads(line)


def modern_meta(version: str = MODERN_VERSION) -> dict:
    return {META_PROTOCOL_VERSION: version, META_CLIENT_CAPABILITIES: {}}


def advertised_flags(result: dict) -> dict[str, bool]:
    caps = result.get("capabilities", {})
    extensions = caps.get("extensions", {})
    return {
        "capabilities.subscriptions": "subscriptions" in caps,
        f"capabilities.extensions['{TASKS_KEY}']": TASKS_KEY in extensions,
        f"capabilities.extensions['{HEARTBEAT_KEY}']": HEARTBEAT_KEY in extensions,
    }


def declared_contract(server: Server) -> dict:
    result = server.request("server/discover", {"_meta": modern_meta()})["result"]
    return result["_meta"][META_CAPABILITY_CONTRACT]["declaration"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--json", type=Path, help="also write the JSON report here")
    args = parser.parse_args()

    root = args.repo_root.resolve()
    src = root / "src"
    fixture_repo = root / "tests" / "fixtures" / "atlas" / "sample"
    report = Report()

    manifest = json.loads((root / "contracts" / "mcp-capability-contract.v1.json").read_text())
    matrix = json.loads(
        (root / "contracts" / "fixtures" / "capability-matrix.v1.json").read_text()
    )

    # ── manifest arm ──
    sys.path.insert(0, str(src))
    from scholialang_mcp import capabilities  # noqa: E402

    report.add(
        "manifest",
        "declaration_matches_source_constant",
        manifest["declaration"] == capabilities.CAPABILITY_DECLARATION,
    )
    pins = manifest.get("protocol_pins", {})
    for pin, sha in (
        ("core", "e76e9c572c6f2bfcb730357101acc90f2f802e02"),
        ("tasks", "9263312d11a682ac83f83fe84794d4627efd22f5"),
        ("sdk", "0921d94a74db900dccd2d534842aa7b6160542d2"),
    ):
        report.add("manifest", f"pin_{pin}", pins.get(pin, "").endswith("@" + sha), pins.get(pin, ""))
    receipt = manifest.get("accepted_core_receipt", {})
    report.add(
        "manifest",
        "accepted_core_wheel_receipt",
        receipt.get("wheel_sha256")
        == "bbe08813bb0431824fa82db6b086ff2aafca5f6024e0b377dcfa7d37c25c1831",
    )
    report.add(
        "manifest",
        "blocking_feature_gaps_present",
        all(
            gap.get("blocking") is True
            for gap in manifest.get("feature_gaps", [])
        )
        and {g["id"] for g in manifest.get("feature_gaps", [])}
        >= {"gap-core-073-vendoring", "gap-durable-store", "gap-real-adapters"},
    )
    report.add(
        "manifest",
        "shipped_facets_unimplemented",
        all(
            manifest["declaration"]["facets"][facet]["implemented"] is False
            and manifest["declaration"]["facets"][facet]["conformance_certified"] is False
            for facet in FACETS
        ),
    )
    report.add("manifest", "support_matrix_eight", len(matrix["support_matrix"]) == 8)
    report.add("manifest", "mode_matrix_twenty_seven", len(matrix["mode_matrix"]) == 27)

    surfaces: dict[str, list[str]] = {
        "wheel": [args.python, "-m", "scholialang_mcp", "--repo-root", str(fixture_repo)],
    }
    for host in ("claude-code", "codex", "ollama"):
        surfaces[f"plugin:{host}"] = [
            args.python,
            str(root / "plugins" / host / "scholialang" / "scripts" / "scholialang_mcp_server.py"),
        ]

    # ── shipped + parity + legacy + protocol arms ──
    declarations: dict[str, dict] = {}
    enforce_env = {var: "enforce" for var in POLICY_ENV.values()}
    for surface, cmd in surfaces.items():
        with Server(cmd, enforce_env, src) as server:
            result = server.request("server/discover", {"_meta": modern_meta()})["result"]
            flags = advertised_flags(result)
            report.add(
                "shipped",
                f"{surface}_advertises_no_extension_even_enforced",
                not any(flags.values()),
                json.dumps(flags),
            )
            declarations[surface] = result["_meta"][META_CAPABILITY_CONTRACT]["declaration"]
            for facet, (method, params) in FACET_PROBES.items():
                response = server.request(method, {"_meta": modern_meta(), **params})
                error = response.get("error", {})
                data = error.get("data") or {}
                report.add(
                    "shipped",
                    f"{surface}_{facet}_refuses_explicitly",
                    error.get("code") == -32601
                    and data.get("facet") == facet
                    and data.get("implemented") is False,
                    json.dumps(response),
                )
            legacy = server.request(
                "initialize", {"protocolVersion": "2025-11-25", "capabilities": {}}
            )
            legacy_caps = legacy.get("result", {}).get("capabilities", {})
            report.add(
                "legacy",
                f"{surface}_legacy_sees_no_extension",
                "extensions" not in legacy_caps and "subscriptions" not in legacy_caps,
                json.dumps(legacy_caps),
            )
            bad = server.request(
                "tools/list", {"_meta": modern_meta(matrix["incompatible_protocol"]["requested"])}
            )
            report.add(
                "protocol",
                f"{surface}_incompatible_version_rejected",
                bad.get("error", {}).get("code")
                == matrix["incompatible_protocol"]["expect_error_code"],
                json.dumps(bad.get("error", {})),
            )
    baseline = declarations["wheel"]
    for surface, declared in declarations.items():
        report.add("parity", f"{surface}_declaration_identical", declared == baseline)
    report.add("parity", "manifest_declaration_identical", baseline == manifest["declaration"])

    # ── synthetic matrix arm (wheel only; explicit test arm) ──
    def synthetic_env(facets: list[str], policies: dict[str, str]) -> dict[str, str]:
        env = {
            "SCHOLIALANG_MCP_SYNTHETIC_FACETS": ",".join(facets),
            "SCHOLIALANG_MCP_SYNTHETIC_SEED": json.dumps(SEED),
            "SCHOLIALANG_MCP_PRINCIPAL": "alice",
            "SCHOLIALANG_MCP_PROJECT": "proj-a",
        }
        for facet, policy in policies.items():
            env[POLICY_ENV[facet]] = policy
        return env

    for combo in matrix["support_matrix"]:
        facets = [facet for facet in FACETS if combo[facet]]
        env = synthetic_env(facets, {facet: "enforce" for facet in FACETS})
        with Server(surfaces["wheel"], env, src) as server:
            result = server.request("server/discover", {"_meta": modern_meta()})["result"]
            report.add(
                "synthetic",
                combo["id"],
                advertised_flags(result) == combo["expect_advertised"],
                json.dumps(advertised_flags(result)),
            )
    for combo in matrix["mode_matrix"]:
        env = synthetic_env(list(FACETS), combo["policies"])
        with Server(surfaces["wheel"], env, src) as server:
            result = server.request("server/discover", {"_meta": modern_meta()})["result"]
            flags = advertised_flags(result)
            expected = {
                manifest["declaration"]["facets"][facet]["advertisement"]: facet
                in combo["expect_active"]
                for facet in FACETS
            }
            report.add("synthetic", combo["id"], flags == expected, json.dumps(flags))

    payload = {
        "runner": "scripts/capability_conformance.py",
        "contract": manifest["contract"],
        "ok": report.ok,
        "total": len(report.checks),
        "failed": sum(1 for check in report.checks if not check["ok"]),
        "checks": report.checks,
    }
    text = json.dumps(payload, indent=2)
    print(text)
    if args.json:
        args.json.write_text(text + "\n")
    return 0 if report.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
