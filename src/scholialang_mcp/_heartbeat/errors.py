"""Reason codes and exceptions for MCP Heartbeat 0.1.

Every fail-closed path names a stable machine-readable reason so a policy
engine can key on it without parsing prose. The string values are
deliberately unchanged from the legacy presence profile: adapters and
consumers already key on ``node_id_mismatch`` / ``boot_id_reuse``, and
inventing new spellings for the same conditions would buy nothing and cost
a translation table. Read them as *participant* and *epoch* — see
``docs/heartbeat-0.1.md`` §3.

Stdlib only, by design.
"""
from __future__ import annotations

from enum import Enum


class ViolationCode(str, Enum):
    """Stable reasons a heartbeat is refused.

    Three families: *document* (structurally wrong), *lineage* (well formed
    but contradicts what this consumer already accepted), and *liveness*
    (nothing is wrong with the document; it just cannot prove presence now).
    """

    # ── document ──────────────────────────────────────────────────
    SCHEMA_INVALID = "schema_invalid"
    UNSUPPORTED_EXTENSION_VERSION = "unsupported_extension_version"
    INVALID_EXPIRY_WINDOW = "invalid_expiry_window"
    EXPIRY_WINDOW_TOO_LONG = "expiry_window_too_long"

    # ── lineage ───────────────────────────────────────────────────
    NODE_ID_MISMATCH = "node_id_mismatch"
    SEQUENCE_ROLLBACK = "sequence_rollback"
    SEQUENCE_CONFLICT = "sequence_conflict"
    BOOT_ID_REUSE = "boot_id_reuse"

    # ── liveness ──────────────────────────────────────────────────
    CLOCK_SKEW_EXCEEDED = "clock_skew_exceeded"
    EXPIRED_ON_ARRIVAL = "expired_on_arrival"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.value


class HeartbeatError(Exception):
    """Base class for every heartbeat error."""

    code: ViolationCode = ViolationCode.SCHEMA_INVALID


class InvalidHeartbeat(HeartbeatError):
    """A candidate document failed structural validation.

    Carries the *complete* violation list rather than only the first, so a
    fixture-driven conformance run can assert an exact set.
    """

    code = ViolationCode.SCHEMA_INVALID

    def __init__(self, violations: list[str]) -> None:
        self.violations = list(violations)
        super().__init__("; ".join(self.violations) or "invalid heartbeat")


class UnsupportedExtensionVersion(HeartbeatError):
    """The document declares an ``extension_version`` this build cannot read."""

    code = ViolationCode.UNSUPPORTED_EXTENSION_VERSION

    def __init__(self, found: object, supported: str) -> None:
        self.found = found
        self.supported = supported
        super().__init__(f"unsupported extension_version {found!r} (supported: {supported})")


__all__ = [
    "HeartbeatError",
    "InvalidHeartbeat",
    "UnsupportedExtensionVersion",
    "ViolationCode",
]
