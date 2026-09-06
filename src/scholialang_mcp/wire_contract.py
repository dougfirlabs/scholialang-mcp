"""Pinned modern wire validation and trusted, per-facet activation policy.

Only host code can supply authority/certification. No environment variable or
request field enables an adapter. Schema validation never fetches remote refs.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Callable

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry

from .durable_store import Scope

VERSION = "2026-07-28"
TASKS = "io.modelcontextprotocol/tasks"
HEARTBEAT = "com.dougfirlabs/heartbeat"
SUBSCRIPTION_ID = "io.modelcontextprotocol/subscriptionId"
CLIENT_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
PROTOCOL_VERSION = "io.modelcontextprotocol/protocolVersion"
IDEMPOTENCY_KEY = "org.scholialang/idempotencyKey"
FACETS = ("events", "tasks", "heartbeat")
SCHEMAS = Path(__file__).with_name("wire_schemas")
PINS = json.loads((SCHEMAS / "RECEIPT.json").read_text())


class WireError(ValueError):
    def __init__(self, reason: str, code: int = -32602, data: dict | None = None):
        super().__init__(reason)
        self.code = code
        self.data = data


@lru_cache(maxsize=None)
def validator(era: str, definition: str) -> Draft202012Validator:
    schema = json.loads((SCHEMAS / f"{era}-schema.json").read_text())
    selected = {"$defs": schema["$defs"], "$ref": f"#/$defs/{definition}"}
    return Draft202012Validator(selected, registry=Registry(), format_checker=FormatChecker())


def validate(era: str, definition: str, value: object) -> None:
    if not validator(era, definition).is_valid(value):
        raise WireError(f"invalid_{definition}")


def implementation_digest() -> str:
    """Binds a host's independent receipt to installed code and schema bytes."""
    root = Path(__file__).parent
    paths = [root / name for name in (
        "server.py", "durable_store.py", "wire_contract.py", "wire_adapters.py",
    )]
    paths += sorted((root / "_heartbeat").glob("*.py"))
    paths += sorted(SCHEMAS.glob("*.json"))
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.relative_to(root).as_posix().encode() + b"\0")
        digest.update(path.read_bytes() + b"\0")
    return digest.hexdigest()


@dataclass(frozen=True)
class AdapterPolicy:
    events: str = "off"
    tasks: str = "off"
    heartbeat: str = "off"

    def __post_init__(self) -> None:
        if any(getattr(self, facet) not in ("off", "observe", "enforce") for facet in FACETS):
            raise ValueError("invalid_adapter_policy")


@dataclass
class HostBinding:
    """A local stdio principal/project, with revocable host-owned authority.

    ``certified`` must independently verify its receipt for these exact bytes
    and protocol pins. Tests inject an explicitly synthetic authority. Shipped
    launchers inject none. This seam does not authenticate a network peer.
    """
    scope: Scope | None = None
    policy: AdapterPolicy = field(default_factory=AdapterPolicy)
    authorize: Callable[[Scope, str, str | None], bool] = lambda scope, facet, key: False
    certified: Callable[[str, str, dict], bool] = lambda facet, digest, pins: False
    revoked: bool = False

    def permits(self, facet: str, digest: str, key: str | None = None) -> bool:
        if self.scope is None or self.revoked or getattr(self.policy, facet) != "enforce":
            return False
        # A broken authority is a denial, never a process-wide fail-open.
        try:
            return (self.authorize(self.scope, facet, key) is True
                    and self.certified(facet, digest, json.loads(json.dumps(PINS))) is True)
        except Exception:
            return False
