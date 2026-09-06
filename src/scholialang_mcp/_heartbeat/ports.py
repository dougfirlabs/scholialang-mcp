"""Transport seams. The core defines these and implements none of them.

Each port is a :class:`typing.Protocol`, so an adapter satisfies it
structurally — no base class to inherit, and therefore no import of a
transport, a web framework, or an MCP SDK into this package. That is the
whole point: the same core is driven by a 2025-06-18 adapter and a
2026-07-28 adapter without knowing either exists.

Adding a port never widens the six-field object
(``docs/heartbeat-0.1.md`` §6).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol, runtime_checkable

from .errors import InvalidHeartbeat
from .model import Heartbeat, IdentityBinding, IdentityClaim, is_digest


@dataclass(frozen=True)
class ChangeHint:
    """A notification that a participant's heartbeat changed.

    Carries no authoritative state — only an address and bounded revision
    metadata. Its single legal effect is to make a consumer refetch. A
    consumer that misses every hint MUST still converge, because its refetch
    deadline derives from the held lease's own expiry.
    """

    address: str
    revision: str
    digest: str

    def to_dict(self) -> dict[str, Any]:
        return {"address": self.address, "revision": self.revision, "digest": self.digest}

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "ChangeHint":
        missing = [k for k in ("address", "revision", "digest") if k not in raw]
        if missing:
            raise InvalidHeartbeat([f"hint missing required field: {k}" for k in missing])
        if not is_digest(raw["digest"]):
            raise InvalidHeartbeat(["hint digest must be a 'sha256:<64 hex>' digest"])
        return cls(address=raw["address"], revision=raw["revision"], digest=raw["digest"])

    @classmethod
    def for_heartbeat(cls, heartbeat: Heartbeat, address: str) -> "ChangeHint":
        return cls(address=address, revision=heartbeat.revision, digest=heartbeat.digest)

    def matches(self, heartbeat: Heartbeat) -> bool:
        """True when ``heartbeat`` is the revision this hint announced.

        A consumer compares rather than trusts: the hint is a claim about a
        resource, and only the refetched document is authoritative.
        """
        return self.revision == heartbeat.revision and self.digest == heartbeat.digest


@runtime_checkable
class HeartbeatPublisher(Protocol):
    """Producer → transport. Makes a document readable by consumers."""

    def publish(self, document: Mapping[str, Any]) -> None:
        """Serve ``document`` as the participant's authoritative heartbeat."""
        ...


@runtime_checkable
class HeartbeatSource(Protocol):
    """Transport → consumer. Reads the authoritative document."""

    def fetch(self, participant_id: str) -> Mapping[str, Any]:
        """Return the current heartbeat for ``participant_id``. Raise on failure."""
        ...


@runtime_checkable
class HintReceiver(Protocol):
    """Transport → consumer. Delivers a change hint."""

    def on_hint(self, hint: ChangeHint) -> None:
        """Handle a change hint; the only legal effect is to schedule a refetch."""
        ...


@runtime_checkable
class IdentityBinder(Protocol):
    """Adapter → consumer. Resolves a claim against an authenticated principal.

    Only an adapter sees a credential, so only an adapter may return
    something other than :attr:`~.model.IdentityBinding.UNVERIFIED`. An
    implementation that cannot determine a principal returns ``UNVERIFIED``;
    it must not fall back to "authenticated because the socket was TLS".
    """

    def bind(self, claim: IdentityClaim) -> IdentityBinding:
        """Report how strongly ``claim`` is tied to an authenticated principal."""
        ...


__all__ = [
    "ChangeHint",
    "HeartbeatPublisher",
    "HeartbeatSource",
    "HintReceiver",
    "IdentityBinder",
]
