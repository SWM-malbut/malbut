"""Provider contract shared by local and remote language models."""

from abc import ABC, abstractmethod
from typing import List, Optional, Sequence

from malbut_agent_server.conversation import (
    ConversationSummary,
    ConversationTurn,
)
from malbut_agent_server.memory import MemoryRecord
from malbut_agent_server.schemas import AgentRequest, ProviderResult
from malbut_agent_server.tools import ToolSpec
from malbut_agent_server.trusted_results import TrustedToolResult


class ProviderError(RuntimeError):
    """Raised when a provider cannot return a valid normalized result."""


class AgentProvider(ABC):
    """Common model adapter interface."""

    @abstractmethod
    def complete(
        self,
        request: AgentRequest,
        memories: List[MemoryRecord],
        conversation_turns: List[ConversationTurn],
        tools: List[ToolSpec],
        conversation_summary: Optional[ConversationSummary] = None,
    ) -> ProviderResult:
        """Return exactly one normalized high-level decision."""
        raise NotImplementedError

    def complete_with_context(
        self,
        request: AgentRequest,
        memories: List[MemoryRecord],
        conversation_turns: List[ConversationTurn],
        tools: List[ToolSpec],
        conversation_summary: Optional[ConversationSummary] = None,
        trusted_server_tool_results: Sequence[TrustedToolResult] = (),
    ) -> ProviderResult:
        """Delegate for legacy adapters that do not consume new context."""
        del trusted_server_tool_results
        return self.complete(
            request,
            memories,
            conversation_turns,
            tools,
            conversation_summary,
        )
