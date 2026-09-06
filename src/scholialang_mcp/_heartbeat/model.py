"""The heartbeat value type and its canonical encoding.

A heartbeat is a frozen value with a deterministic serialization: two
producers that agree on the six contract fields emit byte-identical
canonical JSON and therefore the same digest. That is what lets a change
hint carry a digest a consumer can compare without trusting the hint.

Six fields, no more (``docs/heartbeat-0.1.md`` §2). Readiness, health,
consistency, and pressure are deliberately *not* here — they are consumer
projections, not wire fields, and admitting one would make freshness look
like permission.

Stdlib only. No host-application / goatlib / scholialang imports, ever.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping

from .errors import InvalidHeartbeat, UnsupportedExtensionVersion

#: Version of *this contract*, independent of any MCP protocol revision.
EXTENSION_VERSION = "0.1"

#: Optional, safely ignorable data lives here under namespaced keys.
EXTENSIONS_KEY = "extensions"

#: The mandatory wire members, in schema order.
CORE_FIELDS: tuple[str, ...] = (
    "extension_version",
    "node_id",
    "boot_id",
    "sequence",
    "issued_at",
    "expires_at",
)

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_NODE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]{0,254}$")
_BOOT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


# ── timestamps ────────────────────────────────────────────────────────


def format_rfc3339(moment: datetime) -> str:
    """Render ``moment`` as an RFC 3339 UTC instant with a ``Z`` suffix.

    Wall-clock values on the wire are always UTC for interoperability;
    *scheduling* uses a monotonic clock (see :mod:`.clock`).
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def parse_rfc3339(text: str) -> datetime:
    """Parse an RFC 3339 UTC instant. Raises ``ValueError`` when malformed."""
    if not isinstance(text, str) or not text:
        raise ValueError("timestamp must be a non-empty string")
    candidate = text[:-1] + "+00:00" if text.endswith("Z") else text
    parsed = datetime.fromisoformat(candidate)
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp {text!r} is missing a UTC offset")
    return parsed.astimezone(timezone.utc)


# ── canonical encoding ────────────────────────────────────────────────


def canonical_json(payload: Mapping[str, Any]) -> str:
    """Serialize deterministically: sorted keys, no incidental whitespace."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest_of(payload: Mapping[str, Any]) -> str:
    """``sha256:<hex>`` over the canonical JSON of ``payload``."""
    raw = canonical_json(payload).encode("utf-8")
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def is_digest(value: object) -> bool:
    """True when ``value`` is a well-formed ``sha256:<64 hex>`` string."""
    return isinstance(value, str) and bool(_DIGEST_RE.match(value))


# ── identity ──────────────────────────────────────────────────────────


class IdentityBinding(str, Enum):
    """Whether an adapter tied a claimed participant to a real principal.

    Kept separate from the claim itself so the three states cannot be
    collapsed into a boolean, which is how "the socket was TLS" turns into
    "this participant proved who it is". See ``docs/heartbeat-0.1.md`` §5.
    """

    #: A principal was determined and may publish this participant id.
    BOUND = "bound"
    #: A principal was determined and may **not**. A security event.
    UNBOUND = "unbound"
    #: No principal could be determined. The default; never a promotion.
    UNVERIFIED = "unverified"


@dataclass(frozen=True)
class IdentityClaim:
    """A participant identity as *claimed* by a heartbeat.

    The core can validate this claim's syntax and its continuity across
    epochs. It cannot check who actually published it, so
    :attr:`authenticated` is unconditionally ``False`` and there is no core
    path that sets it otherwise. Only a transport adapter — which is the
    only thing that sees a credential — may report a
    :class:`IdentityBinding` other than ``UNVERIFIED``.
    """

    participant_id: str
    epoch_id: str
    binding: IdentityBinding = IdentityBinding.UNVERIFIED

    @property
    def authenticated(self) -> bool:
        """Always ``False``. A self-claim is never proof of identity."""
        return False


# ── the document ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class Heartbeat:
    """One immutable, ordered liveness lease.

    A revision is identified by ``(boot_id, sequence)`` and fingerprinted by
    :attr:`digest`. Both travel in a change hint so a consumer can tell
    "same revision, redelivered" from "new revision, must refetch".

    ``node_id`` and ``boot_id`` keep their wire spelling at 0.1;
    :attr:`participant_id` and :attr:`epoch_id` are the normative names for
    the same values (``docs/heartbeat-0.1.md`` §3).
    """

    node_id: str
    boot_id: str
    sequence: int
    issued_at: datetime
    expires_at: datetime
    extension_version: str = EXTENSION_VERSION
    extensions: Mapping[str, Any] = field(default_factory=dict)

    # ── normative aliases ────────────────────────────────────────

    @property
    def participant_id(self) -> str:
        """Whatever publishes exactly one totally-ordered heartbeat stream."""
        return self.node_id

    @property
    def epoch_id(self) -> str:
        """The process lifetime this stream belongs to."""
        return self.boot_id

    @property
    def identity(self) -> IdentityClaim:
        """The *claimed* identity. Never authenticated by the core."""
        return IdentityClaim(participant_id=self.node_id, epoch_id=self.boot_id)

    # ── serialization ────────────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        """Render the wire document: the six fields, plus extensions if any."""
        out: dict[str, Any] = {
            "extension_version": self.extension_version,
            "node_id": self.node_id,
            "boot_id": self.boot_id,
            "sequence": self.sequence,
            "issued_at": format_rfc3339(self.issued_at),
            "expires_at": format_rfc3339(self.expires_at),
        }
        if self.extensions:
            out[EXTENSIONS_KEY] = dict(self.extensions)
        return out

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "Heartbeat":
        """Parse and validate a wire document.

        Raises :class:`UnsupportedExtensionVersion` when the declared version
        is unreadable, and :class:`InvalidHeartbeat` carrying the *complete*
        violation list otherwise.
        """
        from .validation import validate_document

        if not isinstance(raw, Mapping):
            raise InvalidHeartbeat(["document must be a JSON object"])
        version = raw.get("extension_version")
        if version != EXTENSION_VERSION:
            raise UnsupportedExtensionVersion(version, EXTENSION_VERSION)

        violations = validate_document(raw)
        if violations:
            raise InvalidHeartbeat(violations)

        return cls(
            node_id=raw["node_id"],
            boot_id=raw["boot_id"],
            sequence=raw["sequence"],
            issued_at=parse_rfc3339(raw["issued_at"]),
            expires_at=parse_rfc3339(raw["expires_at"]),
            extension_version=version,
            extensions=raw.get(EXTENSIONS_KEY) or {},
        )

    # ── revision identity ────────────────────────────────────────

    @property
    def digest(self) -> str:
        """Content fingerprint over the canonical wire document."""
        return digest_of(self.to_dict())

    @property
    def revision(self) -> str:
        """Opaque revision label — ``<boot_id>:<sequence>``."""
        return f"{self.boot_id}:{self.sequence}"

    def remaining_seconds(self, now: datetime) -> float:
        """Seconds of validity left at ``now``; <= 0 means expired."""
        return (self.expires_at - now).total_seconds()

    def is_fresh(self, now: datetime) -> bool:
        """True while ``now`` is strictly inside the claimed window."""
        return self.remaining_seconds(now) > 0


__all__ = [
    "CORE_FIELDS",
    "EXTENSIONS_KEY",
    "EXTENSION_VERSION",
    "Heartbeat",
    "IdentityBinding",
    "IdentityClaim",
    "canonical_json",
    "digest_of",
    "format_rfc3339",
    "is_digest",
    "parse_rfc3339",
]
