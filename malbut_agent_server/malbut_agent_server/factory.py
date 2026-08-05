"""Construct the offline bounded-context runtime."""

from malbut_agent_server.config import Settings
from malbut_agent_server.conversation import SQLiteConversationStore
from malbut_agent_server.memory import SQLiteMemoryStore
from malbut_agent_server.orchestrator import AgentOrchestrator
from malbut_agent_server.providers.mock import MockProvider
from malbut_agent_server.safety import SafetyPolicy


def build_orchestrator(settings: Settings) -> AgentOrchestrator:
    """Build one Mock-only runtime without network dependencies."""
    if settings.provider != 'mock':
        raise ValueError('SWM25-71 supports only the mock provider')
    memory_store = SQLiteMemoryStore(settings.database_path)
    conversation_store = None
    try:
        conversation_store = SQLiteConversationStore(
            settings.database_path,
            ttl_seconds=settings.conversation_ttl_seconds,
            history_limit=settings.conversation_history_limit,
            max_sessions_per_user=(
                settings.max_conversation_sessions
            ),
            max_turns_per_session=settings.max_conversation_turns,
            summary_max_chars=(
                settings.conversation_summary_max_chars
            ),
        )
        return AgentOrchestrator(
            provider=MockProvider(
                max_model_input_chars=(
                    settings.max_model_input_chars
                ),
            ),
            memory_store=memory_store,
            conversation_store=conversation_store,
            safety_policy=SafetyPolicy(),
            memory_limit=settings.memory_limit,
        )
    except Exception:
        if conversation_store is not None:
            conversation_store.close()
        memory_store.close()
        raise
