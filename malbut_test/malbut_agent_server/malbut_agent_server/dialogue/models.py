"""Constants, validation helpers, and internal reply records for DialogueWorker."""

from dataclasses import dataclass
import re
from typing import Any, Callable, Dict, Optional

from malbut_agent_server.schemas import MAX_SPEECH_TRANSCRIPT_LENGTH


ERROR_RESPONSE = '답변을 만들지 못했어요. 다시 말씀해 주세요.'
SESSION_ERROR_RESPONSE = (
    '대화 세션을 사용할 수 없어요. Agent 대화 모드를 다시 시작해 주세요.'
)
MEMORY_CHANGED_RESPONSE = (
    '기억 정보가 변경되어 이전 답변을 전달하지 않았어요. 다시 말씀해 주세요.'
)
MAX_INTERRUPTION_IDS = 128
MAX_SPEECH_ID_LENGTH = 256
NAVIGATION_CONFIRMATION_SECONDS = 30.0
MAPPING_NAME_SECONDS = 120.0
NAVIGATION_CHANGED_RESPONSE = '지도나 방 정보가 변경되어 이동하지 않았어요. 다시 말씀해 주세요.'
ADDRESSEE_DECISIONS = ('addressed', 'not_addressed', 'unknown')
NEW_CONVERSATION_REQUESTS = frozenset({
    '새로시작하자', '새로시작해', '새로시작해줘', '새로시작해주세요',
    '대화새로시작하자', '대화를새로시작하자', '대화를새로시작해줘',
    '대화를새로시작해주세요', '새대화시작하자', '새대화를시작하자',
    '새대화시작해줘', '새대화를시작해줘', '새대화를시작해주세요',
})


def starts_new_conversation(text: str) -> bool:
    """Recognize a direct opening command, preserving any following request."""
    parts = re.split(r'[.!?。！？]+', text, maxsplit=1)
    if re.sub(r'\s+', '', parts[0]) not in NEW_CONVERSATION_REQUESTS:
        return False
    return len(parts) == 1 or not re.match(
        r'\s*(?:[\"\'”’`]|라고|라는|라며|고\s*(?:했|말))', parts[1],
    )


class _DialogueReply(dict):
    """Keep freshness validation outside the serializable reply fields."""

    _memory_validator: Optional[Callable[[], Any]]
    _mission_callback: Optional[Callable[[], str]]
    _mission_request_id: Optional[str]
    _on_publish: Optional[Callable[[], None]]
    _prefetch: Any
    _prepared_hint: Dict[str, Any]
    _delivery_ticket: Optional[Dict[str, Any]]
    _turn_trace: Any
    _waterfall: Any
    _progress: Any
    _generation: int
    _companion_session: Any

    def __init__(
        self,
        fields: Dict[str, Any],
        memory_validator: Optional[Callable[[], Any]] = None,
    ) -> None:
        """Attach the internal guard without adding a public JSON field."""
        super().__init__(fields)
        self._memory_validator = memory_validator
        self._mission_callback = None
        self._mission_request_id = None
        self._on_publish = None
        self._prefetch = None
        self._prepared_hint = {}
        self._delivery_ticket = None
        self._turn_trace = None
        self._waterfall = None
        self._progress = None
        self._companion_session = None


@dataclass(frozen=True)
class _NavigationConfirmation:
    """Pending navigation confirmation bound to a conversation revision."""

    proposal: Any
    conversation_id: str
    generation: int
    revision: int
    expires_at: float


@dataclass(frozen=True)
class _MappingNameQuestion:
    """Pending map-name clarification bound to a conversation revision."""

    conversation_id: str
    generation: int
    revision: int
    expires_at: float


class SpeechInputTooLongError(ValueError):
    """The complete transcript exceeds the supported speech turn size."""


def validate_dialogue_input(utterance_id: str, text: str) -> None:
    """Reject unusable input before the caller commits a receipt."""
    if not isinstance(utterance_id, str) or not utterance_id.strip():
        raise ValueError('utterance_id must be a nonblank string')
    if not isinstance(text, str) or not text.strip():
        raise ValueError('text must be a nonblank string')
    if len(text) > MAX_SPEECH_TRANSCRIPT_LENGTH:
        raise SpeechInputTooLongError(
            f'text exceeds {MAX_SPEECH_TRANSCRIPT_LENGTH} characters; '
            'the complete transcript was not accepted',
        )
    try:
        utterance_id.encode('utf-8')
        text.encode('utf-8')
    except UnicodeEncodeError as error:
        raise ValueError('speech input must be valid UTF-8') from error


def validate_interruption_input(utterance_id: str, playback_id: str, text: str) -> None:
    """Bound correlation IDs and reuse the normal transcript text limits."""
    validate_dialogue_input(utterance_id, text)
    if (not isinstance(playback_id, str) or not playback_id.strip()
            or len(playback_id) > MAX_SPEECH_ID_LENGTH
            or len(utterance_id) > MAX_SPEECH_ID_LENGTH):
        raise ValueError('interruption IDs must be nonblank and bounded')
    try:
        playback_id.encode('utf-8')
    except UnicodeEncodeError as error:
        raise ValueError('playback_id must be valid UTF-8') from error


def make_reply(
    utterance_id: str,
    conversation_id: Optional[str],
    text: str,
    kind: str,
    memory_validator: Optional[Callable[[], Any]] = None,
) -> _DialogueReply:
    """Construct a serializable dialogue reply carrying internal guards."""
    return _DialogueReply(
        {
            'utterance_id': utterance_id,
            'text': text,
            'kind': kind,
            'conversation_id': conversation_id,
        },
        memory_validator,
    )


def addressee_reply(
    utterance_id: str,
    playback_id: str,
    decision: str,
) -> Dict[str, str]:
    """Build an internal addressee classification result dictionary."""
    return {
        'kind': 'addressee',
        'utterance_id': utterance_id,
        'playback_id': playback_id,
        'decision': decision,
    }
