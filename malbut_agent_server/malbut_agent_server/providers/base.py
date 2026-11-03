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


def close_providers(*providers):
    """Close distinct owned providers, including after another close fails."""
    seen = set()
    failure = None
    for provider in providers:
        if id(provider) in seen:
            continue
        seen.add(id(provider))
        close = getattr(provider, 'close', None)
        if callable(close):
            try:
                close()
            except Exception as error:
                if failure is None:
                    failure = error
    if failure is not None:
        raise failure


def accepts_memory_context(provider: object) -> bool:
    """Check opt-in and the actual method before passing a new keyword."""
    if not getattr(provider, 'supports_memory', False):
        return False
    return _accepts_context_keyword(provider, 'memory_context')


def accepts_weather_context(provider: object) -> bool:
    """Check the signature without retrying a legacy call after TypeError."""
    return _accepts_context_keyword(provider, 'weather_context')


def _accepts_context_keyword(provider: object, name: str) -> bool:
    """Inspect keyword compatibility without invoking provider code."""
    try:
        parameters = inspect.signature(provider.complete).parameters
    except (AttributeError, TypeError, ValueError):
        return False
    parameter = parameters.get(name)
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
        weather_context: Optional[dict] = None,
    ) -> ProviderResult:
        """Return exactly one normalized high-level decision."""
        raise NotImplementedError
