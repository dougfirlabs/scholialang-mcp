"""MCP Heartbeat — a small, transport-neutral liveness-lease contract.

A heartbeat asserts one thing: *this participant was alive and publishing at
``issued_at``, and claims the window through ``expires_at``.* It never
asserts readiness, health, authorization, correctness, or task success.
Freshness is not permission.

Six mandatory wire fields, an injected clock, no threads, no network client,
no persistence, and no dependency outside the Python standard library. The
whole contract is ``docs/heartbeat-0.1.md`` plus
``schema/mcp-heartbeat-0.1.schema.json``; this package is its reference
implementation.

``extension_version`` versions *this contract* and is a separate axis from
the MCP protocol revision. Adapters for a given protocol era live outside
this package and plug in through :mod:`mcp_heartbeat.ports`.

    >>> from mcp_heartbeat import FakeClock, HeartbeatIssuer, LineageState, admit
    >>> clock = FakeClock()
    >>> issuer = HeartbeatIssuer(participant_id="svc/api-7", epoch_id="e1", clock=clock)
    >>> state = LineageState(participant_id="svc/api-7")
    >>> admit(state, issuer.issue().to_dict(), clock.now()).accepted
    True
"""
from __future__ import annotations

from . import clock, errors, issuer, lineage, model, ports, validation
from .clock import *  # noqa: F403
from .errors import *  # noqa: F403
from .issuer import *  # noqa: F403
from .lineage import *  # noqa: F403
from .model import *  # noqa: F403
from .ports import *  # noqa: F403
from .validation import *  # noqa: F403

__version__ = "0.1.0"

# Composed from each module's own ``__all__`` rather than restated, so the
# package surface cannot drift from the modules' — a second hand-maintained
# list is a bug waiting for the next atom to be added.
__all__ = ["__version__"] + sorted(
    name
    for module in (clock, errors, issuer, lineage, model, ports, validation)
    for name in module.__all__
)
