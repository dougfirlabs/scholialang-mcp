"""Frozen MCP capability, identity and durable-state contracts (sch073-04).

Three extension facets — ``events`` (core 2026-07-28 subscriptions),
``tasks`` (io.modelcontextprotocol/tasks) and ``heartbeat``
(com.dougfirlabs/heartbeat lease 0.1) — are negotiated independently.
None of them is implemented on the shipped server: discovery must never
advertise an unfinished facet, and every facet defaults to policy
``off``. Synthetic adapters exist solely so the conformance matrix can
prove the negotiation contract on a disposable test server; their wire
advertisement is explicitly marked ``synthetic`` so a mocked arm can
never be mistaken for shipped transport support.

The declared contract below must stay byte-equal (as canonical JSON)
across the wheel server, all three plugin servers, and
``contracts/mcp-capability-contract.v1.json`` — enforced by
``tests/integration/test_capability_contract.py`` and
``scripts/capability_conformance.py``.
"""

from __future__ import annotations

import copy
import json
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Optional

CAPABILITY_CONTRACT_NAME = "scholialang.mcp-capabilities.v1"
# Namespaced, safely-ignorable discovery metadata key (an OT integration
# seam — NOT a substitute for MCP wire negotiation).
META_CAPABILITY_CONTRACT = "com.dougfirlabs/scholialang.mcp-capabilities.v1"

POLICY_OFF = "off"
POLICY_OBSERVE = "observe"
POLICY_ENFORCE = "enforce"
VALID_POLICIES = (POLICY_OFF, POLICY_OBSERVE, POLICY_ENFORCE)

FACET_EVENTS = "events"
FACET_TASKS = "tasks"
FACET_HEARTBEAT = "heartbeat"
FACETS = (FACET_EVENTS, FACET_TASKS, FACET_HEARTBEAT)

POLICY_ENV = {
    FACET_EVENTS: "SCHOLIALANG_MCP_EXT_EVENTS_POLICY",
    FACET_TASKS: "SCHOLIALANG_MCP_EXT_TASKS_POLICY",
    FACET_HEARTBEAT: "SCHOLIALANG_MCP_EXT_HEARTBEAT_POLICY",
}

# Principal/project bind from trusted launcher configuration only. For a
# stdio server that is the host's server config block — a *configured local
# principal*, never an authenticated network peer by implication.
ENV_PRINCIPAL = "SCHOLIALANG_MCP_PRINCIPAL"
ENV_PROJECT = "SCHOLIALANG_MCP_PROJECT"

# Test-arm activation only. Comma-separated facet names; anything not in
# FACETS is ignored (fail closed). Never set by shipped host configs.
ENV_SYNTHETIC_FACETS = "SCHOLIALANG_MCP_SYNTHETIC_FACETS"
ENV_SYNTHETIC_SEED = "SCHOLIALANG_MCP_SYNTHETIC_SEED"

# JSON-RPC error codes (implementation-defined server range).
PRINCIPAL_UNBOUND = -32000
SCOPED_NOT_FOUND = -32001

TASKS_CAPABILITY_KEY = "io.modelcontextprotocol/tasks"
HEARTBEAT_CAPABILITY_KEY = "com.dougfirlabs/heartbeat"

# Wire methods per facet, from the pinned revisions in
# .ralph/contracts/protocol-contract-pins.md. Nothing here invents an RPC:
# events/tasks methods are the pinned stable names; the heartbeat facet's
# synthetic probe is deliberately vendor-namespaced.
EXTENSION_METHOD_FACETS = {
    "subscriptions/listen": FACET_EVENTS,
    "tasks/get": FACET_TASKS,
    "tasks/update": FACET_TASKS,
    "tasks/cancel": FACET_TASKS,
    "com.dougfirlabs/heartbeat.lease": FACET_HEARTBEAT,
}

# The declared capability contract. This dict describes the SHIPPED
# implementation state and must be identical for every server surface;
# per-surface facts (server name, protocol-version matrix, synthetic
# arms) live outside it. Keep in lockstep with the plugin servers'
# CAPABILITY_DECLARATION block and contracts/mcp-capability-contract.v1.json.
CAPABILITY_DECLARATION: dict[str, Any] = {
    "contract": CAPABILITY_CONTRACT_NAME,
    "package_version": "0.7.2",
    "grammar_version": "0.6.2",
    "engine_validator_version": "0.7.2",
    "core_dependency": {
        "declared_floor": "scholialang>=0.7.2,<0.8",
        "accepted_candidate": {
            "version": "0.7.3",
            "source_sha": "9a86a4645c49074c4a415ade01093bff0e2ca70c",
            "wheel_sha256": "bbe08813bb0431824fa82db6b086ff2aafca5f6024e0b377dcfa7d37c25c1831",
            "sdist_sha256": "457fe675175adf2c3166eeb55ffe86f8e9e0fb72b5acea54615ac3401c2557b2",
            "status": "receipt_pinned_not_yet_vendored",
        },
    },
    "protocol": {"preferred": "2026-07-28"},
    "policy": {
        "values": list(VALID_POLICIES),
        "default": POLICY_OFF,
        "environment": dict(POLICY_ENV),
        "invalid_value_behavior": "fail_closed_to_off",
        "observe_semantics": "records negotiation attempts; never executes, advertises, or approves",
        "enforce_semantics": "activates only an implemented, independently conformance-certified adapter",
    },
    "facets": {
        FACET_EVENTS: {
            "wire_methods": ["subscriptions/listen"],
            "advertisement": "capabilities.subscriptions",
            "pin": "modelcontextprotocol/modelcontextprotocol@e76e9c572c6f2bfcb730357101acc90f2f802e02",
            "implemented": False,
            "conformance_certified": False,
            "negotiation": "independent",
        },
        FACET_TASKS: {
            "wire_methods": ["tasks/get", "tasks/update", "tasks/cancel"],
            "advertisement": "capabilities.extensions['io.modelcontextprotocol/tasks']",
            "pin": "modelcontextprotocol/experimental-ext-tasks@9263312d11a682ac83f83fe84794d4627efd22f5",
            "implemented": False,
            "conformance_certified": False,
            "negotiation": "independent",
        },
        FACET_HEARTBEAT: {
            "wire_methods": ["com.dougfirlabs/heartbeat.lease"],
            "advertisement": "capabilities.extensions['com.dougfirlabs/heartbeat']",
            "pin": "com.dougfirlabs/mcp-heartbeat@cb87379bbe4e26a8e59bab9cc18dc51ef4079bff:packages/mcp-heartbeat",
            "implemented": False,
            "conformance_certified": False,
            "negotiation": "independent",
        },
    },
    "identity": {
        "principal_source": "trusted_configuration_only",
        "caller_supplied_identity": "never_authoritative",
        "unbound_context": "fail_closed",
        "id_axes": "MCP task ids, OT run ids, A2A ids, approval ids and DAG/atom ids stay distinct",
    },
}


def declaration() -> dict[str, Any]:
    """Deep copy so callers can never mutate the shared declaration."""
    return copy.deepcopy(CAPABILITY_DECLARATION)


def canonical_declaration_json(payload: Optional[Mapping[str, Any]] = None) -> str:
    source = CAPABILITY_DECLARATION if payload is None else payload
    return json.dumps(source, sort_keys=True, separators=(",", ":"))


def resolve_policies(env: Optional[Mapping[str, str]] = None) -> dict[str, str]:
    """Per-facet policy from trusted environment configuration.

    Unset, empty, or unrecognized values fail closed to ``off`` — a
    misspelled ``enforce`` must never activate a facet, and a policy
    downgrade can never certify support (advertisement additionally
    requires an implemented, certified adapter).
    """
    source = os.environ if env is None else env
    policies: dict[str, str] = {}
    for facet in FACETS:
        raw = (source.get(POLICY_ENV[facet]) or "").strip().lower()
        policies[facet] = raw if raw in VALID_POLICIES else POLICY_OFF
    return policies


@dataclass(frozen=True)
class PrincipalContext:
    """Authenticated principal + project/tenant from trusted configuration."""

    principal: str
    project: str
    source: str = "configured_stdio"


def resolve_principal(env: Optional[Mapping[str, str]] = None) -> Optional[PrincipalContext]:
    """``None`` means unbound: every scoped operation must fail closed."""
    source = os.environ if env is None else env
    principal = (source.get(ENV_PRINCIPAL) or "").strip()
    project = (source.get(ENV_PROJECT) or "").strip()
    if not principal or not project:
        return None
    return PrincipalContext(principal=principal, project=project)


class ScopeDenied(Exception):
    """Cross-principal/project access. Callers must render this exactly
    like a missing record so foreign callers cannot probe existence."""


class ExtensionMethodError(Exception):
    def __init__(self, code: int, message: str, data: Optional[dict[str, Any]] = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data


class ScopedMemoryStore:
    """Synthetic, in-memory, test-only store — NOT the durable contract.

    It does model the promised transaction boundary: writes stage inside
    ``transaction()`` and notification intents are surfaced only after
    commit (notifications-after-commit). Every record is namespaced by
    ``(facet, principal, project)``; reads and mutations outside that
    scope raise :class:`ScopeDenied`. Retention is bounded by
    ``max_records`` per scope. The authoritative durable-store selection
    is an explicitly unresolved feature gap recorded in the contract
    manifest.
    """

    def __init__(self, max_records: int = 256):
        self._records: dict[tuple[str, str, str], dict[str, dict[str, Any]]] = {}
        self._revisions: dict[tuple[str, str, str], int] = {}
        self.max_records = max_records
        self.committed_notifications: list[dict[str, Any]] = []

    @staticmethod
    def _scope(facet: str, context: PrincipalContext) -> tuple[str, str, str]:
        return (facet, context.principal, context.project)

    def put(
        self,
        facet: str,
        context: PrincipalContext,
        record_id: str,
        record: dict[str, Any],
        *,
        notify: Optional[dict[str, Any]] = None,
    ) -> int:
        scope = self._scope(facet, context)
        bucket = self._records.setdefault(scope, {})
        if record_id not in bucket and len(bucket) >= self.max_records:
            raise ExtensionMethodError(
                -32603, "scope retention bound exceeded", {"facet": facet}
            )
        bucket[record_id] = dict(record)
        self._revisions[scope] = self._revisions.get(scope, 0) + 1
        # Single authoritative commit point: state first, then intent.
        if notify is not None:
            self.committed_notifications.append(
                {"facet": facet, "scope_revision": self._revisions[scope], **notify}
            )
        return self._revisions[scope]

    def get(self, facet: str, context: PrincipalContext, record_id: str) -> dict[str, Any]:
        for (f, principal, project), bucket in self._records.items():
            if f == facet and record_id in bucket:
                if principal != context.principal or project != context.project:
                    raise ScopeDenied(record_id)
                return dict(bucket[record_id])
        raise KeyError(record_id)

    def list_ids(self, facet: str, context: PrincipalContext) -> list[str]:
        return sorted(self._records.get(self._scope(facet, context), {}))

    def revision(self, facet: str, context: PrincipalContext) -> int:
        return self._revisions.get(self._scope(facet, context), 0)


@dataclass
class ExtensionAdapter:
    """One negotiable facet implementation behind the registry.

    ``advertised_payload`` is what discovery exposes when the facet is
    active; synthetic adapters must set ``synthetic: True`` inside it.
    ``handler(method, params, context)`` returns a result dict or raises
    :class:`ExtensionMethodError`.
    """

    facet: str
    implemented: bool
    conformance_certified: bool
    advertised_payload: dict[str, Any]
    handler: Callable[[str, dict[str, Any], PrincipalContext], dict[str, Any]]
    synthetic: bool = False


@dataclass
class ExtensionRegistry:
    adapters: dict[str, ExtensionAdapter] = field(default_factory=dict)

    def register(self, adapter: ExtensionAdapter) -> None:
        if adapter.facet not in FACETS:
            raise ValueError(f"unknown facet: {adapter.facet}")
        self.adapters[adapter.facet] = adapter

    def active(self, facet: str, policies: Mapping[str, str]) -> bool:
        """A facet is active only when independently implemented,
        conformance-certified AND explicitly enforced. Policy alone can
        never manufacture support; an adapter alone never activates."""
        adapter = self.adapters.get(facet)
        return (
            adapter is not None
            and adapter.implemented
            and adapter.conformance_certified
            and policies.get(facet) == POLICY_ENFORCE
        )

    def capabilities_payload(self, policies: Mapping[str, str]) -> dict[str, Any]:
        """Discovery ``capabilities`` — advertises exactly the active
        facets, each independently, and nothing else."""
        payload: dict[str, Any] = {"tools": {"listChanged": False}}
        extensions: dict[str, Any] = {}
        if self.active(FACET_EVENTS, policies):
            payload["subscriptions"] = dict(self.adapters[FACET_EVENTS].advertised_payload)
        if self.active(FACET_TASKS, policies):
            extensions[TASKS_CAPABILITY_KEY] = dict(self.adapters[FACET_TASKS].advertised_payload)
        if self.active(FACET_HEARTBEAT, policies):
            extensions[HEARTBEAT_CAPABILITY_KEY] = dict(
                self.adapters[FACET_HEARTBEAT].advertised_payload
            )
        if extensions:
            payload["extensions"] = extensions
        return payload

    def synthetic_facets(self, policies: Mapping[str, str]) -> list[str]:
        return sorted(
            facet
            for facet, adapter in self.adapters.items()
            if adapter.synthetic and self.active(facet, policies)
        )


def refusal_data(
    facet: str,
    policy: str,
    registry: ExtensionRegistry,
    *,
    reason: str = "facet_not_active",
) -> dict[str, Any]:
    """Explicit fallback payload for an inactive facet — never a false
    success, never an advertisement."""
    adapter = registry.adapters.get(facet)
    return {
        "facet": facet,
        "policy": policy,
        "implemented": bool(adapter is not None and adapter.implemented),
        "observed": policy == POLICY_OBSERVE,
        "reason": reason,
    }


# ── Synthetic conformance adapters (test arm only) ───────────────────────


def _require_str(params: Mapping[str, Any], key: str) -> str:
    value = params.get(key)
    if not isinstance(value, str) or not value:
        raise ExtensionMethodError(-32602, f"{key} must be a non-empty string")
    return value


def _synthetic_events_handler(store: ScopedMemoryStore):
    def handle(method: str, params: dict[str, Any], context: PrincipalContext) -> dict[str, Any]:
        requested = params.get("notifications")
        if requested is not None and not isinstance(requested, list):
            raise ExtensionMethodError(-32602, "notifications filter must be an array")
        subscription_id = f"sub-{store.revision(FACET_EVENTS, context) + 1}"
        store.put(
            FACET_EVENTS,
            context,
            subscription_id,
            {"filters": requested or [], "principal": context.principal},
            notify={"kind": "subscription_acknowledged", "subscription_id": subscription_id},
        )
        return {"synthetic": True, "subscriptionId": subscription_id, "acknowledgedFilters": requested or []}

    return handle


def _synthetic_tasks_handler(store: ScopedMemoryStore):
    def handle(method: str, params: dict[str, Any], context: PrincipalContext) -> dict[str, Any]:
        task_id = _require_str(params, "taskId")
        # Caller-supplied ownership claims are never authoritative: the
        # bound context alone decides scope, so a forged owner/project
        # field changes nothing.
        try:
            record = store.get(FACET_TASKS, context, task_id)
        except (KeyError, ScopeDenied):
            # Identical shape for missing and foreign records: no
            # cross-tenant existence oracle.
            raise ExtensionMethodError(
                SCOPED_NOT_FOUND, "task not found", {"facet": FACET_TASKS}
            )
        if method == "tasks/get":
            return {"synthetic": True, "resultType": "complete", "task": record}
        if method == "tasks/update":
            responses = params.get("inputResponses")
            if not isinstance(responses, dict):
                raise ExtensionMethodError(-32602, "inputResponses must be a JSON object")
            record["inputResponses"] = responses
            store.put(FACET_TASKS, context, task_id, record, notify={"kind": "task_updated", "task_id": task_id})
            return {"synthetic": True, "resultType": "complete", "task": record}
        # tasks/cancel: acknowledge intent; execution may race to completion.
        record["cancelRequested"] = True
        store.put(FACET_TASKS, context, task_id, record, notify={"kind": "task_cancel_requested", "task_id": task_id})
        return {"synthetic": True, "resultType": "complete"}

    return handle


def _synthetic_heartbeat_handler(store: ScopedMemoryStore):
    def handle(method: str, params: dict[str, Any], context: PrincipalContext) -> dict[str, Any]:
        sequence = store.revision(FACET_HEARTBEAT, context) + 1
        # Deterministic synthetic clock: lease validity semantics belong
        # to the real adapter job, not this negotiation probe.
        lease = {
            "extension_version": "0.1",
            "node_id": f"synthetic:{context.principal}",
            "boot_id": "00000000-0000-4000-8000-000000000000",
            "sequence": sequence,
            "issued_at": "1970-01-01T00:00:00Z",
            "expires_at": "1970-01-01T00:00:30Z",
        }
        store.put(FACET_HEARTBEAT, context, f"lease-{sequence}", lease)
        return {"synthetic": True, **lease}

    return handle


def load_synthetic_seed(env: Optional[Mapping[str, str]] = None) -> list[dict[str, Any]]:
    source = os.environ if env is None else env
    raw = source.get(ENV_SYNTHETIC_SEED)
    if not raw:
        return []
    try:
        seed = json.loads(raw)
    except json.JSONDecodeError:
        return []
    if not isinstance(seed, list):
        return []
    return [entry for entry in seed if isinstance(entry, dict)]


def build_registry(env: Optional[Mapping[str, str]] = None) -> ExtensionRegistry:
    """Shipped default: an EMPTY registry — no facet implemented, nothing
    advertised, whatever the policies say. Synthetic adapters attach only
    when the test-arm environment names their facets explicitly."""
    source = os.environ if env is None else env
    registry = ExtensionRegistry()
    requested = {
        token.strip().lower()
        for token in (source.get(ENV_SYNTHETIC_FACETS) or "").split(",")
        if token.strip()
    }
    facets = [facet for facet in FACETS if facet in requested]
    if not facets:
        return registry
    store = ScopedMemoryStore()
    for entry in load_synthetic_seed(source):
        facet = entry.get("facet")
        principal = entry.get("principal")
        project = entry.get("project")
        record_id = entry.get("id")
        record = entry.get("record")
        if (
            facet in FACETS
            and isinstance(principal, str)
            and isinstance(project, str)
            and isinstance(record_id, str)
            and isinstance(record, dict)
        ):
            store.put(facet, PrincipalContext(principal, project, source="seed"), record_id, record)
    builders = {
        FACET_EVENTS: _synthetic_events_handler,
        FACET_TASKS: _synthetic_tasks_handler,
        FACET_HEARTBEAT: _synthetic_heartbeat_handler,
    }
    for facet in facets:
        registry.register(
            ExtensionAdapter(
                facet=facet,
                implemented=True,
                conformance_certified=True,
                advertised_payload={"synthetic": True},
                handler=builders[facet](store),
                synthetic=True,
            )
        )
    return registry
