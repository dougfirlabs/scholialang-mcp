"""Injectable clocks — the reason these tests never sleep.

Time is split in two on purpose:

* the **wall clock** produces the RFC 3339 ``issued_at`` / ``expires_at`` on
  the wire, so two hosts can compare instants;
* the **monotonic clock** drives local renewal and expiry scheduling, so a
  wall-clock jump (NTP step, VM resume) cannot make a live participant look
  expired or an expired one look fresh.

Both sit behind :class:`Clock`, so every function in this package is pure
with respect to time. The core starts no thread and schedules nothing; a
caller decides when to ask. :class:`FakeClock` advances by assignment.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Protocol


class Clock(Protocol):
    """A wall/monotonic clock pair."""

    def now(self) -> datetime:
        """Current wall-clock instant, timezone-aware UTC."""
        ...

    def monotonic(self) -> float:
        """Seconds from an arbitrary fixed origin; never moves backwards."""
        ...


class SystemClock:
    """The real clock. The only place ``time``/``datetime.now`` is called."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def monotonic(self) -> float:
        return time.monotonic()


class FakeClock:
    """Deterministic clock for tests and fixture replay.

    Wall and monotonic advance together under :meth:`advance`, which is the
    honest default. :meth:`skew_wall` moves *only* the wall clock — that is
    how the skew fixtures reproduce an NTP step without disturbing local
    scheduling.
    """

    def __init__(
        self,
        start: datetime | None = None,
        monotonic_start: float = 1000.0,
    ) -> None:
        self._wall = start or datetime(2026, 1, 1, tzinfo=timezone.utc)
        self._monotonic = float(monotonic_start)

    def now(self) -> datetime:
        return self._wall

    def monotonic(self) -> float:
        return self._monotonic

    def advance(self, seconds: float) -> None:
        """Move both clocks forward by ``seconds``."""
        if seconds < 0:
            raise ValueError("FakeClock cannot advance backwards")
        self._wall = self._wall + timedelta(seconds=seconds)
        self._monotonic += seconds

    def skew_wall(self, seconds: float) -> None:
        """Step the wall clock only — monotonic time is untouched."""
        self._wall = self._wall + timedelta(seconds=seconds)


__all__ = ["Clock", "FakeClock", "SystemClock"]
