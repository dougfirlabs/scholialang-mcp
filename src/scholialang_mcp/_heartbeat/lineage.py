"""Admission: does this candidate heartbeat supersede what we already hold?

This is the whole judgement surface of the core, and it is a pure function
of (held state, candidate document, current wall instant). No clock is read
here, no I/O is performed, and no thread is started — a caller supplies
``now`` from an injected :class:`~.clock.Clock`, which is why every case
below is deterministic under :class:`~.clock.FakeClock`.

The check order in :func:`admit` is normative
(``docs/heartbeat-0.1.md`` §4). In particular lineage is checked *before*
skew: a replayed old revision is also stale, so a skew-first order would
report every replay as ``clock_skew_exceeded`` and hide the rollback that
actually happened.

Every rejection preserves the held state. This fails closed.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Any, Mapping

from .errors import ViolationCode
from .model import Heartbeat
from .validation import document_reason

#: Default bound on ``|issued_at - now|``. Wide enough for ordinary NTP
#: drift, narrow enough that a replayed lease cannot claim the present.
DEFAULT_MAX_SKEW_SECONDS = 5.0


@dataclass(frozen=True)
class LineageState:
    """What a consumer remembers about one participant.

    ``retired_epochs`` grows monotonically and is the replay defence: an
    epoch that has ever been seen may never reappear.
    """

    participant_id: str
    held: Heartbeat | None = None
    retired_epochs: frozenset[str] = field(default_factory=frozenset)


@dataclass(frozen=True)
class Admission:
    """Outcome of one admission attempt.

    ``duplicate`` marks byte-identical redelivery: idempotent, neither a
    transition nor an error, so it carries no reason.
    """

    state: LineageState
    reason: ViolationCode | None = None
    duplicate: bool = False

    @property
    def accepted(self) -> bool:
        """True when the candidate became the held heartbeat."""
        return self.reason is None and not self.duplicate


def check_lineage(state: LineageState, candidate: Heartbeat) -> ViolationCode | None:
    """Compare ``candidate`` against the held stream.

    Returns ``None`` to accept. A duplicate is *not* a violation, so callers
    must first ask :func:`is_duplicate`.
    """
    held = state.held
    if held is None or candidate.boot_id != held.boot_id:
        # A new or first epoch. Reappearance of a retired one is the reuse
        # attack: it would let a replayed old epoch masquerade as current.
        return ViolationCode.BOOT_ID_REUSE if candidate.boot_id in state.retired_epochs else None

    if candidate.sequence < held.sequence:
        return ViolationCode.SEQUENCE_ROLLBACK
    if candidate.sequence == held.sequence:
        # Same counter, different bytes: two writers under one identity.
        return None if candidate.digest == held.digest else ViolationCode.SEQUENCE_CONFLICT
    return None


def is_duplicate(state: LineageState, candidate: Heartbeat) -> bool:
    """True when ``candidate`` is the held revision redelivered verbatim."""
    held = state.held
    return (
        held is not None
        and candidate.boot_id == held.boot_id
        and candidate.sequence == held.sequence
        and candidate.digest == held.digest
    )


def check_freshness(
    candidate: Heartbeat,
    now: datetime,
    *,
    max_skew_seconds: float = DEFAULT_MAX_SKEW_SECONDS,
) -> ViolationCode | None:
    """Reject a heartbeat issued too far from ``now`` or already expired."""
    if abs((candidate.issued_at - now).total_seconds()) > max_skew_seconds:
        return ViolationCode.CLOCK_SKEW_EXCEEDED
    if not candidate.is_fresh(now):
        return ViolationCode.EXPIRED_ON_ARRIVAL
    return None


def admit(
    state: LineageState,
    document: Mapping[str, Any],
    now: datetime,
    *,
    max_skew_seconds: float = DEFAULT_MAX_SKEW_SECONDS,
) -> Admission:
    """Run the five normative checks in order and return the outcome."""
    reason = document_reason(document)
    if reason is not None:
        return Admission(state, reason)

    candidate = Heartbeat.from_dict(document)
    if candidate.node_id != state.participant_id:
        # A routing check, not an authentication check: it says the document
        # arrived at the wrong tracker, never that the publisher is genuine.
        return Admission(state, ViolationCode.NODE_ID_MISMATCH)

    if is_duplicate(state, candidate):
        return Admission(state, duplicate=True)

    reason = check_lineage(state, candidate)
    if reason is not None:
        return Admission(state, reason)

    reason = check_freshness(candidate, now, max_skew_seconds=max_skew_seconds)
    if reason is not None:
        return Admission(state, reason)

    return Admission(
        replace(
            state,
            held=candidate,
            retired_epochs=state.retired_epochs | {candidate.boot_id},
        )
    )


__all__ = [
    "DEFAULT_MAX_SKEW_SECONDS",
    "Admission",
    "LineageState",
    "admit",
    "check_freshness",
    "check_lineage",
    "is_duplicate",
]
