"""Run existing conversation handling outside the ROS callback thread."""

from collections import deque
import hashlib
from threading import Condition, Thread
from typing import Callable, Optional

from malbut_agent_server.conversation import (
    ConversationNotFoundError, ConversationStateError,
)
from malbut_agent_server.schemas import (
    AgentRequest, MAX_UTTERANCE_LENGTH, RobotState, validate_user_id,
)


ERROR_RESPONSE = '답변을 만들지 못했어요. 다시 말씀해 주세요.'
SESSION_ERROR_RESPONSE = (
    '대화 세션을 사용할 수 없어요. Agent 대화 모드를 다시 시작해 주세요.'
)


def validate_dialogue_input(utterance_id: str, text: str) -> None:
    """Reject unusable input before the caller commits a receipt."""
    if not isinstance(utterance_id, str) or not utterance_id.strip():
        raise ValueError('utterance_id must be a nonblank string')
    if not isinstance(text, str) or not text.strip():
        raise ValueError('text must be a nonblank string')
    if len(text) > MAX_UTTERANCE_LENGTH:
        raise ValueError(f'text exceeds {MAX_UTTERANCE_LENGTH} characters')
    try:
        utterance_id.encode('utf-8')
        text.encode('utf-8')
    except UnicodeEncodeError as error:
        raise ValueError('speech input must be valid UTF-8') from error


class DialogueWorker:
    """Keep one conversation ordered and bound queued and unread work.

    ``runtime_factory`` constructs the existing AgentOrchestrator in this
    worker. Its stores are closed here after the final running call returns.
    The ROS thread retains ownership of receipt storage and text publication.
    """

    def __init__(
        self, runtime_factory: Callable, user_id: str, capacity: int = 10,
    ):
        if not callable(runtime_factory):
            raise TypeError('runtime_factory must be callable')
        if type(capacity) is not int or capacity < 1:
            raise ValueError('capacity must be a positive integer')
        self._factory = runtime_factory
        self._user_id = validate_user_id(user_id)
        self._capacity = capacity
        self._condition = Condition()
        self._pending = deque()
        self._results = deque()
        self._outstanding = 0
        self._closing = False
        self._stopped = False
        self._startup_error: Optional[str] = None
        self._thread = Thread(target=self._run, name='malbut-speech-dialogue')
        self._thread.start()

    @property
    def startup_error(self) -> Optional[str]:
        """Expose an initialization failure without provider error details."""
        with self._condition:
            return self._startup_error

    def has_capacity(self) -> bool:
        """Count pending, in-flight and unread utterances together."""
        with self._condition:
            return (
                not self._closing and not self._stopped
                and self._outstanding < self._capacity
            )

    def submit(self, utterance_id: str, text: str) -> bool:
        """Queue an original utterance without waiting for model inference."""
        validate_dialogue_input(utterance_id, text)
        with self._condition:
            if not self.has_capacity():
                return False
            self._outstanding += 1
            self._pending.append((utterance_id, text))
            self._condition.notify()
            return True

    def drain(self) -> list[dict]:
        """Return ready replies and release their admission capacity."""
        with self._condition:
            results = list(self._results)
            self._results.clear()
            self._outstanding -= len(results)
            return results

    def close(self) -> None:
        """Discard waiting and late replies; let the running call finish."""
        with self._condition:
            self._closing = True
            self._pending.clear()
            self._results.clear()
            self._outstanding = 0
            self._condition.notify_all()
        self._thread.join()

    def _run(self):
        runtime = None
        conversation_id = None
        try:
            try:
                runtime = self._factory()
                session = runtime.conversation_store.create(self._user_id)
                conversation_id = session.conversation_id
            except Exception as error:
                with self._condition:
                    self._startup_error = type(error).__name__
                    self._stopped = True
                    if not self._closing:
                        while self._pending:
                            utterance_id, _text = self._pending.popleft()
                            self._results.append(self._reply(
                                utterance_id, conversation_id,
                                ERROR_RESPONSE, 'error',
                            ))
                return
            while True:
                with self._condition:
                    self._condition.wait_for(
                        lambda: self._closing or bool(self._pending),
                    )
                    if self._closing:
                        return
                    utterance_id, text = self._pending.popleft()
                try:
                    digest = hashlib.sha256(
                        utterance_id.encode('utf-8'),
                    ).hexdigest()
                    result = runtime.handle(AgentRequest(
                        request_id='speech-request-' + digest,
                        user_id=self._user_id,
                        conversation_id=conversation_id,
                        turn_id='speech-turn-' + digest,
                        utterance=text,
                        robot_state=RobotState(), available_tools=(),
                    ))
                    decision = result.decision
                    if (decision.type not in {
                            'message', 'clarification', 'refusal',
                    } or not isinstance(decision.message, str)
                            or not decision.message.strip()):
                        raise ValueError('Expected a non-action response')
                    reply = self._reply(
                        utterance_id, conversation_id,
                        decision.message, 'answer',
                    )
                except (ConversationNotFoundError, ConversationStateError):
                    reply = self._reply(
                        utterance_id, conversation_id,
                        SESSION_ERROR_RESPONSE, 'error',
                    )
                except Exception:
                    reply = self._reply(
                        utterance_id, conversation_id, ERROR_RESPONSE, 'error',
                    )
                with self._condition:
                    if not self._closing:
                        self._results.append(reply)
        finally:
            with self._condition:
                self._stopped = True
            if runtime is not None:
                try:
                    runtime.conversation_store.close()
                finally:
                    runtime.memory_store.close()

    @staticmethod
    def _reply(utterance_id, conversation_id, text, kind):
        return {
            'utterance_id': utterance_id,
            'text': text,
            'kind': kind,
            'conversation_id': conversation_id,
        }
