from __future__ import annotations

from scholialang_mcp.providers.base import ProviderRequest, ProviderResponse


class OllamaProvider:
    name = "ollama"

    def invoke(self, request: ProviderRequest) -> ProviderResponse:
        return ProviderResponse(
            status="not_configured",
            metadata={"provider": self.name, "prompt_length": len(request.prompt)},
        )

