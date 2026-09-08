"""Provider contract shared by local and remote language models."""

from abc import ABC, abstractmethod
import inspect
from typing import List, Optional

from malbut_agent_server.conversation import (
    ConversationSummary,
    ConversationTurn,
)
from malbut_agent_server.memory import MemoryRecord
from malbut_agent_server.schemas import AgentRequest, ProviderResult
from malbut_agent_server.tools import ToolSpec


class ProviderError(RuntimeError):
    """Raised when a provider cannot return a valid normalized result."""


def accepts_memory_context(provider: object) -> bool:
    """Check opt-in and the actual method before passing a new keyword."""
    if not getattr(provider, 'supports_memory', False):
        return False
    try:
        parameters = inspect.signature(provider.complete).parameters
    except (AttributeError, TypeError, ValueError):
        return False
    parameter = parameters.get('memory_context')
    if parameter is not None:
        return parameter.kind in {
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        }
    return any(parameter.kind is inspect.Parameter.VAR_KEYWORD
               for parameter in parameters.values())


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
        *,
        memory_context: Optional[dict] = None,
    ) -> ProviderResult:
        """Return exactly one normalized high-level decision."""
        raise NotImplementedError
