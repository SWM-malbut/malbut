"""Run existing conversation handling outside the ROS callback thread."""

from collections import OrderedDict, deque
from concurrent.futures import CancelledError
from dataclasses import dataclass
import hashlib
import math
import re
import sqlite3
from threading import Condition, Thread
import time
from typing import Callable, Optional

from malbut_agent_server.conversation import (
    ConversationNotFoundError, ConversationStateError,
)
from malbut_agent_server.conversation_progress import RequestProgress, request_scope
from malbut_agent_server.function_speech import FUNCTION_STARTS
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
NAVIGATION_CONFIRMATION_SECONDS = 30.0
NAVIGATION_CHANGED_RESPONSE = '지도나 방 정보가 변경되어 이동하지 않았어요. 다시 말씀해 주세요.'
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
        self._mission_callback = None
        self._on_publish = None


@dataclass(frozen=True)
class _NavigationConfirmation:
    proposal: object
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
        *, missions=None,
    ):
        """Prepare turns off-thread and optionally dispatch through Manager."""
        if not callable(runtime_factory):
            raise TypeError('runtime_factory must be callable')
        if type(capacity) is not int or capacity < 1:
            raise ValueError('capacity must be a positive integer')
        self._factory = runtime_factory
        self._missions = missions
        self._navigation_confirmation = None
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
        self._suspended = False
        self._generation = 0
        self._running = False
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

    @property
    def has_pending(self) -> bool:
        """Include queued, running, and completed replies awaiting drain."""
        with self._condition:
            return self._outstanding > 0

    def has_capacity(self) -> bool:
        """Count pending, in-flight and unread utterances together."""
        with self._condition:
            return (
                not self._closing and not self._stopped
                and not self._suspended
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
            self._outstanding -= sum(item['kind'] not in {'progress', 'acknowledgement'}
                                     for item in results)
            results = [item for item in results
                       if item['kind'] not in {'progress', 'acknowledgement'}
                       or item._progress.can_publish(item['text'])]
            for reply in results:
                self._refresh_reply(reply)
            return results

    def publish_reply(self, reply: dict, publish: Callable) -> Optional[dict]:
        """Check again immediately before publishing while stores are open."""
        with self._condition:
            if (self._closing or self._stopped or self._suspended
                    or getattr(reply, '_generation', self._generation)
                    != self._generation):
                return None
            if reply.get('kind') == 'addressee':
                return None
            if reply.get('kind') in {'progress', 'acknowledgement'}:
                return dict(reply) if reply._progress.publish(publish, reply['text']) else None
            self._refresh_reply(reply)
            dispatch = getattr(reply, '_mission_callback', None)
            if dispatch is not None:
                # Consume before calling transport, even if publication fails.
                reply._mission_callback = None
                try:
                    reply['text'] = dispatch()
                except Exception:
                    reply['text'] = '실행 요청의 처리 여부를 확인하지 못했어요. 자동으로 다시 보내지 않을게요.'
                    reply['kind'] = 'error'
            if publish(reply['text']):
                activate = reply._on_publish
                reply._on_publish = None
                if activate is not None:
                    activate()
                return dict(reply)
            return None

    def suspend(self):
        """Preempt ordinary speech and invalidate queued or in-flight replies."""
        with self._condition:
            self._suspended = True
            self._navigation_confirmation = None
            self._generation += 1
            if self._active_progress is not None:
                self._active_progress.finish(cancelled=True)
            for _, _, _, progress in self._pending:
                if progress is not None:
                    progress.finish(cancelled=True)
            self._pending.clear()
            self._results.clear()
            self._interruptions.clear()
            self._outstanding = int(self._running)

    def resume(self):
        """Accept new ordinary turns without replaying preempted replies."""
        with self._condition:
            self._suspended = False

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
            reply._mission_callback = None
            reply._on_publish = None
        except Exception:
            reply['text'] = ERROR_RESPONSE
            reply['kind'] = 'error'
            reply._memory_validator = None
            reply._mission_callback = None
            reply._on_publish = None

    def close(self) -> None:
        """Discard waiting and late replies; let the running call finish."""
        with self._condition:
            self._closing = True
            self._navigation_confirmation = None
            if self._active_progress is not None:
                self._active_progress.finish(cancelled=True)
            for _, _, _, progress in self._pending:
                if progress is not None:
                    progress.finish(cancelled=True)
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
                                progress.finish(cancelled=True)
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
                    confirmation = None
                    if playback_id is None:
                        confirmation = self._navigation_confirmation
                        self._navigation_confirmation = None
                    generation = self._generation
                    self._running = True
                    self._active_progress = progress
                if playback_id is not None:
                    reply = self._classify_interruption(
                        runtime, conversation_id, utterance_id, playback_id, text,
                    )
                    with self._condition:
                        self._running = False
                        if not self._closing and generation != self._generation:
                            self._outstanding -= 1
                        elif not self._closing:
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
                    confirmation = self._valid_navigation_confirmation(session, confirmation)
                    locations = (self._missions.navigation_locations()
                                 if self._missions is not None else None)
                    if (confirmation is not None
                            and confirmation.proposal.location not in (locations or ())):
                        confirmation = None
                    navigation_binding = (self._missions.prepare(
                        request_id, 'request_navigation', {'location': locations[0]},
                    ) if locations else None)
                    request = SpeechAgentRequest(
                        request_id=request_id,
                        user_id=self._user_id,
                        conversation_id=conversation_id,
                        turn_id='speech-turn-' + digest,
                        utterance=text,
                        robot_state=RobotState(),
                        available_tools=tuple(getattr(runtime, 'speech_mission_tools', ()))
                        + tuple(getattr(runtime, 'homecam_query_tools', ())) + (
                            ('get_weather', 'set_weather_location')
                            if getattr(runtime, 'weather_executor', None)
                            is not None else ()
                        ),
                        navigation_locations=locations,
                        navigation_confirmation=(confirmation.proposal.location
                                                 if confirmation is not None else ''),
                    )
                    result = self._handle_with_progress(runtime, request, progress)
                    decision = result.decision
                    if (navigation_binding is not None
                            and result.raw_decision.tool_name == 'request_navigation'
                            and not self._missions.navigation_matches(navigation_binding)):
                        reply = self._reply(utterance_id, conversation_id,
                                            NAVIGATION_CHANGED_RESPONSE, 'answer')
                    elif decision.type == 'tool_call' and self._missions is not None:
                        reply = self._mission_reply(
                            runtime, request, result, utterance_id, conversation_id,
                            confirmation=confirmation, navigation_binding=navigation_binding,
                        )
                    elif (decision.type not in {
                            'message', 'clarification', 'refusal',
                    } or not isinstance(decision.message, str)
                            or not decision.message.strip()):
                        raise ValueError('Expected a non-action response')
                    else:
                        message = decision.message
                        if ((decision.type == 'refusal' and decision.reason in {
                                'safety:unknown_tool', 'safety:tool_unavailable',
                        }) or (decision.type == 'message' and decision.reason in {
                                'unsupported_capability', 'unavailable_capability',
                        })):
                            from malbut_agent_server.mission_audio import CATALOG
                            message = CATALOG['operation.unsupported']
                        reply = self._reply(
                            utterance_id, conversation_id,
                            message, 'answer',
                            getattr(result, 'memory_validator', None),
                        )
                        if (self._missions is not None
                                and result.safety.code == 'navigation_confirmation_required'):
                            self._prepare_navigation_confirmation(runtime, request, result, reply)
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
                    progress.finish(cancelled=reply is None)
                with self._condition:
                    self._active_progress = None
                    self._running = False
                    if not self._closing:
                        if reply is None or generation != self._generation:
                            self._outstanding -= 1
                        else:
                            reply._generation = generation
                            from malbut_agent_server.mission_audio import TEXT_IDS
                            if reply['text'] not in TEXT_IDS:
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

    def _valid_navigation_confirmation(self, session, pending):
        """Validate the confirmation consumed by this ordinary turn at dequeue."""
        with self._condition:
            if (pending is not None and time.monotonic() < pending.expires_at
                    and session.status == 'active'
                    and session.conversation_id == pending.conversation_id
                    and session.generation == pending.generation
                    and session.revision == pending.revision
                    and self._missions.navigation_matches(pending.proposal)):
                return pending
            return None

    def _prepare_navigation_confirmation(self, runtime, request, result, reply):
        """Bind the question to its target and activate only after publication."""
        if result.raw_decision.type != 'tool_call':
            # A cached clarification has no fresh proposal and cannot re-arm it.
            return
        proposal = self._missions.prepare(
            request.request_id, 'request_navigation', result.raw_decision.arguments,
        )

        def activate():
            expires_at = time.monotonic() + NAVIGATION_CONFIRMATION_SECONDS
            if self._running or self._pending:
                # An answer received before this question is not confirmation.
                return
            try:
                session = runtime.conversation_store.snapshot(
                    self._user_id, request.conversation_id, limit=1,
                ).session
            except (ConversationNotFoundError, ConversationStateError, sqlite3.Error):
                return
            if (session.status == 'active'
                    and session.generation == result.conversation_generation
                    and session.revision == result.conversation_revision
                    and self._missions.navigation_matches(proposal)
                    and time.monotonic() < expires_at):
                self._navigation_confirmation = _NavigationConfirmation(
                    proposal, request.conversation_id, session.generation, session.revision,
                    expires_at,
                )

        reply._on_publish = activate

    def _mission_reply(self, runtime, request, result, utterance_id, conversation_id,
                       *, confirmation=None, navigation_binding=None):
        """Bind a committed proposal; send only at the ROS publication boundary."""
        decision = result.decision
        if (decision.tool_name not in getattr(runtime, 'speech_mission_tools', ())
                or not result.safety.allowed or result.safety.code != 'manager_request'):
            raise ValueError('unsupported speech mission proposal')
        proposal = self._missions.prepare(
            request.request_id, decision.tool_name, decision.arguments,
        )
        reply = self._reply(
            utterance_id, conversation_id, proposal.message, 'answer',
            getattr(result, 'memory_validator', None),
        )

        def guard():
            if (decision.tool_name == 'request_navigation' and navigation_binding is not None
                    and not self._missions.navigation_matches(navigation_binding)):
                return NAVIGATION_CHANGED_RESPONSE
            if (confirmation is not None and decision.tool_name == 'request_navigation'
                    and proposal.location == confirmation.proposal.location
                    and not self._missions.navigation_matches(confirmation.proposal)):
                return '확인 중 지도나 목적지가 변경되었거나 시간이 지났어요. 다시 요청해 주세요.'
            if result.memory_validator is not None:
                result.memory_validator()
            session = runtime.conversation_store.snapshot(
                self._user_id, conversation_id, limit=1,
            ).session
            if (session.status != 'active'
                    or session.generation != result.conversation_generation
                    or session.revision != result.conversation_revision):
                return '대화가 변경되어 이전 실행 요청을 보내지 않았어요.'
            # SQLite validation can wait; measure freshness after those reads.
            now = float(result.clock())
            if (not math.isfinite(now) or now < result.issued_at
                    or now >= result.expires_at):
                return '요청의 유효 시간이 지나 실행하지 않았어요. 다시 말씀해 주세요.'
            if (confirmation is not None and decision.tool_name == 'request_navigation'
                    and proposal.location == confirmation.proposal.location
                    and time.monotonic() >= confirmation.expires_at):
                return '목적지 확인 시간이 지났어요. 다시 요청해 주세요.'
            return None

        reply._mission_callback = lambda: self._missions.dispatch(proposal, guard=guard)
        return reply

    def _progress_notice(self, utterance_id, text, progress):
        with self._condition:
            if self._closing or not progress.active:
                return
            kind = 'acknowledgement' if text in FUNCTION_STARTS.values() else 'progress'
            reply = self._reply(utterance_id, None, text, kind)
            reply._progress = progress
            reply._generation = self._generation
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
