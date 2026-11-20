"""Domain value objects, exceptions, and constants for conversation storage."""

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from malbut_agent_server.schemas import ValidationError


MAX_RESPONSE_JSON_LENGTH = 65536
DEFAULT_SUMMARY_MAX_CHARS = 2000
SUMMARY_UPDATE_BATCH_SIZE = 128
CONFIRMATION_STORAGE_SCHEMA_VERSION = 1
MAX_CONFIRMATION_JSON_LENGTH = 32768
TEXT_TURN_CLAIM_SCHEMA_VERSION = 1
TEXT_TURN_CLAIM_OUTCOMES = frozenset({
    'confirmation_unrecognized',
    'confirmation_not_pending',
    'confirmation_resolved',
    'confirmation_invalidated',
})


class ConversationNotFoundError(ValidationError):
    """Raised when a user-scoped conversation does not exist."""


class ConversationStateError(ValidationError):
    """Raised when a closed or expired conversation receives a turn."""


class ConversationConflictError(ValidationError):
    """Raised when a request or turn identifier is reused differently."""


class ConversationChangedError(ValidationError):
    """Raised when a session changes while model inference is running."""


class ConfirmationSchemaError(RuntimeError):
    """Raised when confirmation persistence cannot be trusted."""


class ConfirmationIntentNotFoundError(ValidationError):
    """Raised when no owner-scoped confirmation can be selected."""


class ConfirmationIntentConflictError(ValidationError):
    """Raised when a confirmation identity is reused differently."""


class ConfirmationIntentAlreadyTerminalError(ValidationError):
    """Raised when another response already made the intent terminal."""


@dataclass(frozen=True)
class ConversationSession:
    """One user-scoped conversation lifecycle record."""

    conversation_id: str
    user_id: str
    session_instance_id: str
    status: str
    generation: int
    revision: int
    created_at: float
    updated_at: float
    expires_at: float

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-safe session summary."""
        return {
            'conversation_id': self.conversation_id,
            'user_id': self.user_id,
            'session_instance_id': self.session_instance_id,
            'status': self.status,
            'generation': self.generation,
            'revision': self.revision,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
            'expires_at': self.expires_at,
        }


def is_context_limit_response(response):
    """Recognize the server's input rejection, never a model's reason alone."""
    public = response.get('public', response)
    if not isinstance(public, dict):
        return False
    provider, decision = public.get('provider', {}), public.get('decision', {})
    return (isinstance(provider, dict) and isinstance(decision, dict)
            and provider.get('provider') == 'reliable-fallback'
            and provider.get('model') == 'safe-non-action'
            and decision.get('type') == 'refusal'
            and decision.get('reason') == 'conversation_context_limit')


@dataclass(frozen=True)
class ConversationTurn:
    """One committed user and assistant exchange."""

    conversation_id: str
    user_id: str
    session_instance_id: str
    turn_id: str
    request_id: str
    request_fingerprint: str
    generation: int
    ordinal: int
    user_content: str
    assistant_content: str
    response: Dict[str, Any]
    created_at: float
    completed_at: float

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-safe turn without raw provider proposals."""
        return {
            'conversation_id': self.conversation_id,
            'session_instance_id': self.session_instance_id,
            'turn_id': self.turn_id,
            'request_id': self.request_id,
            'generation': self.generation,
            'ordinal': self.ordinal,
            'user': self.user_content,
            'assistant': self.assistant_content,
            'decision': dict(
                self.response.get('decision', {})
            ),
            'created_at': self.created_at,
            'completed_at': self.completed_at,
        }

    def to_messages(self) -> List[Dict[str, Any]]:
        """Return ordered user and assistant message records."""
        return [
            {
                'sequence': self.ordinal * 2 - 1,
                'turn_id': self.turn_id,
                'role': 'user',
                'content': self.user_content,
                'created_at': self.created_at,
            },
            {
                'sequence': self.ordinal * 2,
                'turn_id': self.turn_id,
                'role': 'assistant',
                'content': self.assistant_content,
                'created_at': self.completed_at,
            },
        ]


@dataclass(frozen=True)
class ConversationSummary:
    """Bounded derived context for turns older than the raw window."""

    summary_id: str
    user_id: str
    conversation_id: str
    session_instance_id: str
    generation: int
    summary_revision: int
    content: str
    source_start_ordinal: int
    source_end_ordinal: int
    source_turn_count: int
    source_digest: str
    summarizer: str
    fallback_used: bool
    created_at: float
    updated_at: float

    def to_dict(self) -> Dict[str, Any]:
        """Return summary content with complete source provenance."""
        return {
            'summary_id': self.summary_id,
            'user_id': self.user_id,
            'conversation_id': self.conversation_id,
            'session_instance_id': self.session_instance_id,
            'generation': self.generation,
            'summary_revision': self.summary_revision,
            'content': self.content,
            'source_start_ordinal': self.source_start_ordinal,
            'source_end_ordinal': self.source_end_ordinal,
            'source_turn_count': self.source_turn_count,
            'source_digest': self.source_digest,
            'summarizer': self.summarizer,
            'fallback_used': self.fallback_used,
            'created_at': self.created_at,
            'updated_at': self.updated_at,
        }


@dataclass(frozen=True)
class BeginTurnToken:
    """Immutable compare-and-swap token for one in-flight turn."""

    user_id: str
    conversation_id: str
    session_instance_id: str
    turn_id: str
    request_id: str
    request_fingerprint: str
    generation: int
    revision: int
    ordinal: int


@dataclass(frozen=True)
class BeginTurnResult:
    """Either a new in-flight token or a durable cached response."""

    session: ConversationSession
    history: Tuple[ConversationTurn, ...]
    summary: Optional[ConversationSummary]
    token: Optional[BeginTurnToken]
    cached_response: Optional[Dict[str, Any]]


@dataclass(frozen=True)
class ConversationSnapshot:
    """One generation-consistent session, history, and summary view."""

    session: ConversationSession
    turns: Tuple[ConversationTurn, ...]
    summary: Optional[ConversationSummary]


@dataclass
class ConversationConfig:
    """Shared runtime limits and feature flags for conversation repositories."""

    ttl_seconds: int = 3600
    history_limit: int = 10
    max_sessions_per_user: int = 100
    max_turns_per_session: int = 1000
    summary_max_chars: int = DEFAULT_SUMMARY_MAX_CHARS
    semantic_context: bool = False


@dataclass(frozen=True)
class TextTurnRequestClaim:
    """Durable, content-free claim in the unified text request namespace."""

    user_id: str
    request_id: str
    conversation_id: str
    session_instance_id: str
    generation: int
    revision: int
    turn_id: str
    request_fingerprint: str
    outcome: str
    confirmation_request_id: Optional[str]
    created_at: float
