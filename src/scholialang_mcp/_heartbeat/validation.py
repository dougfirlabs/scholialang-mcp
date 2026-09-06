"""Stdlib validator for heartbeat documents.

``schema/mcp-heartbeat-0.1.schema.json`` is the machine-readable contract for
*external* tooling. This module is the same contract in Python, so the
reference implementation validates with zero third-party dependencies — a
JSON Schema validator is not a runtime requirement of this package.
``tests/test_schema_parity.py`` pins the two together.

Every check returns a message rather than raising, so a rejection reports the
*complete* violation set; fixture-driven conformance asserts exact sets, not
just the first failure.
"""
from __future__ import annotations

from typing import Any, Mapping

from .errors import ViolationCode
from .model import (
    CORE_FIELDS,
    EXTENSION_VERSION,
    EXTENSIONS_KEY,
    _BOOT_ID_RE,
    _NODE_ID_RE,
    parse_rfc3339,
)

#: All six are mandatory. This tuple is the contract's whole required set.
REQUIRED_FIELDS: tuple[str, ...] = CORE_FIELDS

#: Longest validity window accepted. A heartbeat is a heartbeat, not a
#: lifetime grant; an hour-long "lease" would defeat expiry-driven
#: fail-closed behaviour.
MAX_LEASE_SECONDS = 3600.0


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _namespaced(key: object) -> bool:
    """Optional members must be namespaced, i.e. contain a dot."""
    return "." in str(key)


def validate_document(raw: Mapping[str, Any]) -> list[str]:
    """Return every structural violation in ``raw``; empty means valid."""
    out: list[str] = []
    if not isinstance(raw, Mapping):
        return ["document must be a JSON object"]

    for name in REQUIRED_FIELDS:
        if name not in raw:
            out.append(f"missing required field: {name}")

    version = raw.get("extension_version")
    if version is not None and version != EXTENSION_VERSION:
        out.append(f"extension_version must be {EXTENSION_VERSION!r}, got {version!r}")

    node_id = raw.get("node_id")
    if node_id is not None and not (
        isinstance(node_id, str) and _NODE_ID_RE.match(node_id)
    ):
        out.append("node_id must be a scoped opaque identifier matching the profile pattern")

    boot_id = raw.get("boot_id")
    if boot_id is not None and not (
        isinstance(boot_id, str) and _BOOT_ID_RE.match(boot_id)
    ):
        out.append("boot_id must be an opaque identifier matching the profile pattern")

    sequence = raw.get("sequence")
    if sequence is not None:
        if not _is_int(sequence):
            out.append("sequence must be an integer")
        elif sequence < 0:
            out.append("sequence must be >= 0")

    issued_at = expires_at = None
    for name in ("issued_at", "expires_at"):
        value = raw.get(name)
        if value is None:
            continue
        try:
            parsed = parse_rfc3339(value)
        except (TypeError, ValueError):
            out.append(f"{name} must be an RFC 3339 UTC timestamp")
            continue
        if name == "issued_at":
            issued_at = parsed
        else:
            expires_at = parsed

    if issued_at is not None and expires_at is not None:
        window = (expires_at - issued_at).total_seconds()
        if window <= 0:
            out.append("expires_at must be strictly after issued_at")
        elif window > MAX_LEASE_SECONDS:
            out.append(f"lease window must be <= {MAX_LEASE_SECONDS:.0f}s")

    extensions = raw.get(EXTENSIONS_KEY)
    if extensions is not None:
        if not isinstance(extensions, Mapping):
            out.append("extensions must be an object")
        else:
            for key in extensions:
                if not _namespaced(key):
                    out.append(
                        f"extensions key {key!r} must be namespaced (e.g. 'org.example')"
                    )

    # Unknown members are permitted only when namespaced, so a typo in a core
    # field cannot slip through disguised as a safely-ignorable extension.
    for key in raw:
        if key in REQUIRED_FIELDS or key == EXTENSIONS_KEY:
            continue
        if not _namespaced(key):
            out.append(f"unknown member {key!r} must be namespaced to be ignorable")

    return out


def expiry_window_violation(raw: Mapping[str, Any]) -> ViolationCode | None:
    """Return the specific expiry-window code, if the window is the problem.

    Split out of :func:`validate_document` so a rejection can name
    ``invalid_expiry_window`` / ``expiry_window_too_long`` rather than the
    generic ``schema_invalid`` — those two are load-bearing in the threat
    model and deserve their own reason on the wire.
    """
    try:
        issued_at = parse_rfc3339(raw["issued_at"])
        expires_at = parse_rfc3339(raw["expires_at"])
    except (KeyError, TypeError, ValueError):
        return None
    window = (expires_at - issued_at).total_seconds()
    if window <= 0:
        return ViolationCode.INVALID_EXPIRY_WINDOW
    if window > MAX_LEASE_SECONDS:
        return ViolationCode.EXPIRY_WINDOW_TOO_LONG
    return None


def document_reason(raw: Mapping[str, Any]) -> ViolationCode | None:
    """The single most specific reason to reject ``raw``, or ``None``.

    Ordered most-specific-first: an unreadable version is reported as such
    rather than as a pile of "missing field" noise.
    """
    if not isinstance(raw, Mapping):
        return ViolationCode.SCHEMA_INVALID
    if raw.get("extension_version") != EXTENSION_VERSION:
        return ViolationCode.UNSUPPORTED_EXTENSION_VERSION
    window = expiry_window_violation(raw)
    if window is not None:
        return window
    if validate_document(raw):
        return ViolationCode.SCHEMA_INVALID
    return None


__all__ = [
    "MAX_LEASE_SECONDS",
    "REQUIRED_FIELDS",
    "document_reason",
    "expiry_window_violation",
    "validate_document",
]
