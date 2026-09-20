"""Outbound port for replaceable video-language model providers."""

from abc import ABC, abstractmethod

from malbut_agent_server.domain.vlm import (
    VlmAnalysisRequest,
    VlmProviderResult,
)


class VlmProviderError(RuntimeError):
    """A content-free VLM provider failure safe for logs and reports."""


class VlmProvider(ABC):
    """Analyze one incident observation using a provider-neutral request."""

    @abstractmethod
    def analyze(self, request: VlmAnalysisRequest) -> VlmProviderResult:
        """Return one raw common-schema prediction or raise a safe error."""
        raise NotImplementedError
