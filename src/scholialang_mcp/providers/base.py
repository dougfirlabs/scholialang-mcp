"""Provider adapter contract for atlas generation hosts."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class ProviderRequest:
    prompt: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderResponse:
    status: str
    text: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class Provider(Protocol):
    name: str

    def invoke(self, request: ProviderRequest) -> ProviderResponse:
        """Invoke the provider for a Scholia atlas generation task."""

