"""Producer side: mint an ordered stream of heartbeats for one participant.

The issuer owns exactly two pieces of state — the epoch it was constructed
with and the counter within it — and reads time only through an injected
:class:`~.clock.Clock`. It starts no thread and publishes nothing on its
own: a caller decides when to :meth:`~HeartbeatIssuer.issue` and hands the
result to a :class:`~.ports.HeartbeatPublisher`. That keeps renewal
scheduling a transport concern and keeps this file deterministic.
"""
from __future__ import annotations

import secrets
from datetime import timedelta
from typing import Any, Mapping

from .clock import Clock, SystemClock
from .model import Heartbeat

#: Default validity window. Short enough that a dead participant is noticed
#: promptly, long enough to survive one missed renewal.
DEFAULT_LEASE_SECONDS = 30.0


def mint_epoch_id() -> str:
    """Mint a fresh epoch identifier from a random source.

    Random, never derived: an epoch computed from hostname, config, container
    name, or a seed re-presents a retired identifier on restart, which a
    consumer correctly classifies as replay (``docs/heartbeat-0.1.md`` §3.4).
    """
    return secrets.token_hex(8)


class HeartbeatIssuer:
    """Mints the ``(participant, epoch)`` heartbeat stream.

    ``sequence`` starts at 0 for the first heartbeat of an epoch and rises by
    one per issue. It never resets within an issuer; a new epoch means a new
    issuer, which is the same rule as "a new process means a new epoch".
    """

    def __init__(
        self,
        *,
        participant_id: str,
        epoch_id: str | None = None,
        clock: Clock | None = None,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
        extensions: Mapping[str, Any] | None = None,
    ) -> None:
        self.participant_id = participant_id
        self.epoch_id = epoch_id or mint_epoch_id()
        self.clock: Clock = clock or SystemClock()
        self.lease_seconds = float(lease_seconds)
        self.extensions: Mapping[str, Any] = dict(extensions or {})
        self._next_sequence = 0

    @property
    def next_sequence(self) -> int:
        """Counter the next :meth:`issue` will use."""
        return self._next_sequence

    def issue(self) -> Heartbeat:
        """Mint the next heartbeat in the stream."""
        issued_at = self.clock.now()
        heartbeat = Heartbeat(
            node_id=self.participant_id,
            boot_id=self.epoch_id,
            sequence=self._next_sequence,
            issued_at=issued_at,
            expires_at=issued_at + timedelta(seconds=self.lease_seconds),
            extensions=dict(self.extensions),
        )
        self._next_sequence += 1
        return heartbeat


__all__ = ["DEFAULT_LEASE_SECONDS", "HeartbeatIssuer", "mint_epoch_id"]
