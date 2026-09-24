"""Run existing conversation handling outside the ROS callback thread."""

from collections import OrderedDict, deque
from concurrent.futures import CancelledError
import hashlib
import re
from threading import Condition, Thread
from typing import Callable, Optional

from malbut_agent_server.conversation import (
    ConversationNotFoundError, ConversationStateError,
)
from malbut_agent_server.conversation_progress import RequestProgress, request_scope
from malbut_agent_server.orchestrator import MemoryChangedError
from malbut_agent_server.schemas import (
    MAX_SPEECH_TRANSCRIPT_LENGTH, RobotState, SpeechAgentRequest,
    validate_user_id,
)


ERROR_RESPONSE = '답변을 만들지 못했어요. 다시 말씀해 주세요.'
SESSION_ERROR_RESPONSE = (
    '대화 세션을 사용할 수 없어요. Agent 대화 모드를 다시 시작해 주세요.'
)
MEMORY_CHANGED_RESPONSE = (
    '기억 정보가 변경되어 이전 답변을 전달하지 않았어요. 다시 말씀해 주세요.'
)
MAX_INTERRUPTION_IDS = 128
MAX_SPEECH_ID_LENGTH = 256
ADDRESSEE_DECISIONS = ('addressed', 'not_addressed', 'unknown')
NEW_CONVERSATION_REQUESTS = frozenset({
    '새로시작하자', '새로시작해', '새로시작해줘', '새로시작해주세요',
    '대화새로시작하자', '대화를새로시작하자', '대화를새로시작해줘',
    '대화를새로시작해주세요', '새대화시작하자', '새대화를시작하자',
    '새대화시작해줘', '새대화를시작해줘', '새대화를시작해주세요',
})


def starts_new_conversation(text):
    """Recognize a direct opening command, preserving any following request."""
    parts = re.split(r'[.!?。！？]+', text, maxsplit=1)
    if re.sub(r'\s+', '', parts[0]) not in NEW_CONVERSATION_REQUESTS:
        return False
    return len(parts) == 1 or not re.match(
        r'\s*(?:[\"\'”’`]|라고|라는|라며|고\s*(?:했|말))', parts[1],
    )


class _DialogueReply(dict):
    """Keep freshness validation outside the serializable reply fields."""

    def __init__(self, fields, memory_validator=None):
        """Attach the internal guard without adding a public JSON field."""
        super().__init__(fields)
        self._memory_validator = memory_validator


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


def validate_interruption_input(utterance_id, playback_id, text):
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
        self._interruptions = OrderedDict()
        self._outstanding = 0
        self._closing = False
        self._stopped = False
        self._ready = False
        self._startup_error: Optional[str] = None
        self._active_progress = None
        self._thread = Thread(target=self._run, name='malbut-speech-dialogue')
        self._thread.start()

    @property
    def ready(self) -> bool:
        """Report usable runtime and session initialization, not thread creation."""
        with self._condition:
            return self._ready and not self._closing and not self._stopped

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
            progress = RequestProgress(
                lambda notice, state: self._progress_notice(utterance_id, notice, state),
            )
            self._pending.append((utterance_id, text, None, progress))
            self._condition.notify()
            return True

    def submit_interruption(self, utterance_id, playback_id, text) -> bool:
        """Classify once on this conversation without admitting a dialogue turn."""
        validate_interruption_input(utterance_id, playback_id, text)
        digest = hashlib.sha256(text.encode('utf-8')).hexdigest()
        with self._condition:
            if self._closing or self._stopped:
                return False
            previous = self._interruptions.get(utterance_id)
            if previous is not None:
                if previous['playback_id'] == playback_id and previous['digest'] == digest:
                    if previous['conflict']:
                        return False
                    if not previous['pending']:
                        if not self.has_capacity():
                            return False
                        self._outstanding += 1
                        self._results.append(self._addressee_reply(
                            utterance_id, playback_id, previous['decision'],
                        ))
                    return True
                previous['conflict'] = True
                for reply in self._results:
                    if reply['kind'] == 'addressee' and reply['utterance_id'] == utterance_id:
                        reply['decision'] = 'unknown'
                return False
            if not self.has_capacity():
                return False
            if len(self._interruptions) >= MAX_INTERRUPTION_IDS:
                expired = next((uid for uid, entry in self._interruptions.items()
                                if not entry['pending']), None)
                if expired is None:
                    return False
                del self._interruptions[expired]
            self._interruptions[utterance_id] = {
                'playback_id': playback_id, 'digest': digest,
                'pending': True, 'conflict': False,
            }
            self._outstanding += 1
            self._pending.append((utterance_id, text, playback_id, None))
            self._condition.notify()
            return True

    def drain(self) -> list[dict]:
        """Recheck ready replies and release their admission capacity."""
        with self._condition:
            results = list(self._results)
            self._results.clear()
            self._outstanding -= sum(item['kind'] != 'progress' for item in results)
            results = [item for item in results if item['kind'] != 'progress'
                       or item._progress.active]
            for reply in results:
                self._refresh_reply(reply)
            return results

    def publish_reply(self, reply: dict, publish: Callable) -> Optional[dict]:
        """Check again immediately before publishing while stores are open."""
        with self._condition:
            if self._closing or self._stopped:
                return None
            if reply.get('kind') == 'addressee':
                return None
            if reply.get('kind') == 'progress':
                return dict(reply) if reply._progress.publish(publish, reply['text']) else None
            self._refresh_reply(reply)
            if publish(reply['text']):
                return dict(reply)
            return None

    def _refresh_reply(self, reply):
        validator = getattr(reply, '_memory_validator', None)
        if validator is None:
            return
        try:
            if self._closing or self._stopped:
                raise MemoryChangedError('dialogue worker is closed')
            validator()
        except MemoryChangedError:
            reply['text'] = MEMORY_CHANGED_RESPONSE
            reply['kind'] = 'error'
            reply._memory_validator = None
        except Exception:
            reply['text'] = ERROR_RESPONSE
            reply['kind'] = 'error'
            reply._memory_validator = None

    def close(self) -> None:
        """Discard waiting and late replies; let the running call finish."""
        with self._condition:
            self._closing = True
            if self._active_progress is not None:
                self._active_progress.finish()
            for _, _, _, progress in self._pending:
                if progress is not None:
                    progress.finish()
            self._pending.clear()
            self._results.clear()
            self._interruptions.clear()
            self._outstanding = 0
            self._condition.notify_all()
        self._thread.join()

    def _run(self):
        runtime = None
        conversation_id = None
        try:
            try:
                runtime = self._factory()
                start_memory = getattr(runtime, 'start_background_memory', None)
                if start_memory is not None:
                    start_memory()
                session = runtime.conversation_store.resume_or_create(self._user_id)
                conversation_id = session.conversation_id
                with self._condition:
                    self._ready = True
            except Exception as error:
                with self._condition:
                    self._startup_error = type(error).__name__
                    self._stopped = True
                    if not self._closing:
                        while self._pending:
                            utterance_id, _text, playback_id, progress = self._pending.popleft()
                            if progress is not None:
                                progress.finish()
                            if playback_id is not None:
                                self._results.append(self._addressee_reply(
                                    utterance_id, playback_id, 'unknown',
                                ))
                            else:
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
                    utterance_id, text, playback_id, progress = self._pending.popleft()
                    self._active_progress = progress
                if playback_id is not None:
                    reply = self._classify_interruption(
                        runtime, conversation_id, utterance_id, playback_id, text,
                    )
                    with self._condition:
                        if not self._closing:
                            entry = self._interruptions[utterance_id]
                            entry['pending'] = False
                            if entry['conflict']:
                                reply['decision'] = 'unknown'
                            entry['decision'] = reply['decision']
                            self._results.append(reply)
                    continue
                try:
                    digest = hashlib.sha256(
                        utterance_id.encode('utf-8'),
                    ).hexdigest()
                    request_id = 'speech-request-' + digest
                    start_new = (
                        starts_new_conversation(text)
                        and not runtime.conversation_store.has_agent_request(
                            self._user_id, request_id,
                        )
                    )
                    session = runtime.conversation_store.resume_or_create(
                        self._user_id, conversation_id, start_new=start_new,
                    )
                    conversation_id = session.conversation_id
                    result = self._handle_with_progress(runtime, SpeechAgentRequest(
                        request_id=request_id,
                        user_id=self._user_id,
                        conversation_id=conversation_id,
                        turn_id='speech-turn-' + digest,
                        utterance=text,
                        robot_state=RobotState(),
                        available_tools=(
                            ('get_weather', 'set_weather_location')
                            if getattr(runtime, 'weather_executor', None)
                            is not None else ()
                        ),
                    ), progress)
                    decision = result.decision
                    if (decision.type not in {
                            'message', 'clarification', 'refusal',
                    } or not isinstance(decision.message, str)
                            or not decision.message.strip()):
                        raise ValueError('Expected a non-action response')
                    reply = self._reply(
                        utterance_id, conversation_id,
                        decision.message, 'answer',
                        getattr(result, 'memory_validator', None),
                    )
                except CancelledError:
                    reply = None
                except (ConversationNotFoundError, ConversationStateError):
                    reply = self._reply(
                        utterance_id, conversation_id,
                        SESSION_ERROR_RESPONSE, 'error',
                    )
                except MemoryChangedError:
                    reply = self._reply(
                        utterance_id, conversation_id,
                        MEMORY_CHANGED_RESPONSE, 'error',
                    )
                except Exception:
                    reply = self._reply(
                        utterance_id, conversation_id, ERROR_RESPONSE, 'error',
                    )
                finally:
                    progress.finish()
                with self._condition:
                    self._active_progress = None
                    if not self._closing:
                        if reply is None:
                            self._outstanding -= 1
                        else:
                            reply['text'] = progress.final_text(reply['text'])
                            self._results.append(reply)
        finally:
            with self._condition:
                self._stopped = True
            if runtime is not None:
                close_runtime = getattr(runtime, 'close', None)
                if close_runtime is not None:
                    close_runtime()
                else:
                    # Retain the existing injected-runtime adapter contract.
                    try:
                        runtime.conversation_store.close()
                    finally:
                        runtime.memory_store.close()

    def _progress_notice(self, utterance_id, text, progress):
        with self._condition:
            if self._closing or not progress.active:
                return
            reply = self._reply(utterance_id, None, text, 'progress')
            reply._progress = progress
            self._results.append(reply)

    @staticmethod
    def _handle_with_progress(runtime, request, progress):
        with request_scope(progress=progress):
            return runtime.handle(request)

    @staticmethod
    def _addressee_reply(utterance_id, playback_id, decision):
        return {
            'kind': 'addressee', 'utterance_id': utterance_id,
            'playback_id': playback_id, 'decision': decision,
        }

    def _classify_interruption(self, runtime, conversation_id, utterance_id, playback_id, text):
        try:
            snapshot = runtime.conversation_store.snapshot(
                self._user_id, conversation_id, limit=10,
            )
            decision = runtime.speech_addressee.classify(text, snapshot)
            if decision not in ADDRESSEE_DECISIONS:
                decision = 'unknown'
        except Exception:
            decision = 'unknown'
        return self._addressee_reply(utterance_id, playback_id, decision)

    @staticmethod
    def _reply(
        utterance_id, conversation_id, text, kind, memory_validator=None,
    ):
        return _DialogueReply({
            'utterance_id': utterance_id,
            'text': text,
            'kind': kind,
            'conversation_id': conversation_id,
        }, memory_validator)
