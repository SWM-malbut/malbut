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
MAPPING_NAME_SECONDS = 120.0
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
        self._mission_request_id = None
        self._on_publish = None
        self._prefetch = None
        self._prepared_hint = {}
        self._delivery_ticket = None


@dataclass(frozen=True)
class _NavigationConfirmation:
    proposal: object
    conversation_id: str
    generation: int
    revision: int
    expires_at: float


@dataclass(frozen=True)
class _MappingNameQuestion:
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
        *, missions=None, scene_source=None, prefetch_enabled=False,
        publish_before_save=False, on_output=None,
    ):
        """Prepare turns off-thread and optionally dispatch through Manager."""
        if not callable(runtime_factory):
            raise TypeError('runtime_factory must be callable')
        if type(capacity) is not int or capacity < 1:
            raise ValueError('capacity must be a positive integer')
        self._factory = runtime_factory
        self._on_output = on_output
        self._missions = missions
        self._scene_source = scene_source
        if type(prefetch_enabled) is not bool:
            raise TypeError('prefetch_enabled must be a boolean')
        self._prefetch_enabled = prefetch_enabled
        if type(publish_before_save) is not bool:
            raise TypeError('publish_before_save must be a boolean')
        self._publish_before_save = publish_before_save
        self._preparations = deque()
        self._prefetch_turns = {}
        self._prefetch_epoch = 0
        self.patrol_memory = None
        from malbut_agent_server.companion_dialogue import CompanionDialogue
        self._companion = CompanionDialogue()
        self._navigation_confirmation = None
        self._mapping_name_question = None
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

    def _notify_reader(self):
        # Optional ROS wakeup only; the owning thread still publishes and saves.
        if self._on_output is not None:
            try:
                self._on_output()
            except Exception:
                pass  # A slow fallback drain also covers shutdown/wakeup races.

    def _emit(self, reply):
        self._results.append(reply)
        self._notify_reader()

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

    def submit(self, utterance_id: str, text: str, *, opener_context=None,
               companion_session=None) -> bool:
        """Queue an original utterance without waiting for model inference."""
        validate_dialogue_input(utterance_id, text)
        with self._condition:
            if not self.has_capacity():
                return False
            self._cancel_prefetch()
            self._outstanding += 1
            progress = RequestProgress(
                lambda notice, state: self._progress_notice(utterance_id, notice, state),
            )
            progress.prefetch_epoch = self._prefetch_epoch
            # This is what the robot actually said, not a fabricated user turn.
            progress.opener_context = opener_context
            progress.companion_session = companion_session
            if companion_session is not None:
                self._companion.begin(companion_session)
            else:
                self._companion.end()
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
                        self._emit(self._addressee_reply(
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

    def _cancel_prefetch(self):
        self._prefetch_epoch += 1
        for turn in tuple(self._prefetch_turns.values()):
            turn.cancel()
        self._prefetch_turns.clear()

    def _queue_preparation(self, event):
        # The ROS thread publishes these separately from speech replies. A
        # dropped optimization must not prevent ordinary final text delivery.
        with self._condition:
            if event.operation == 'cancel':
                self._preparations = deque(item for item in self._preparations
                                           if item.prepared_id != event.prepared_id)
            elif self._closing or self._suspended:
                return False
            if len(self._preparations) >= 32:
                if event.operation != 'cancel':
                    return False
                self._preparations.popleft()
            self._preparations.append(event)
            self._notify_reader()
            return True

    def drain_preparations(self):
        """Return quiet preparation commands, never playback status or replies."""
        with self._condition:
            events = list(self._preparations)
            self._preparations.clear()
            return events

    def cancel_preparations(self):
        """Invalidate quiet work before waiting for an in-flight request to end."""
        with self._condition:
            self._cancel_prefetch()

    def discard_reply(self, reply):
        """Release quiet work when the ROS publication policy suppresses a reply."""
        with self._condition:
            self._release_prefetch(reply)

    def _release_prefetch(self, reply, *, published=False):
        turn = getattr(reply, '_prefetch', None)
        if turn is not None:
            if not published:
                turn.cancel()
            self._prefetch_turns.pop(turn.request_id, None)

    def drain(self, *, validate=True) -> list[dict]:
        """Recheck ready replies and release their admission capacity."""
        with self._condition:
            results = list(self._results)
            self._results.clear()
            self._outstanding -= sum(item['kind'] not in {'progress', 'acknowledgement'}
                                     and getattr(item, '_delivery_ticket', None) is None
                                     for item in results)
            results = [item for item in results
                       if item['kind'] not in {'progress', 'acknowledgement'}
                       or item._progress.can_publish(item['text'])]
        # Never acquire the DB lock while holding the queue lock: the worker
        # may be handing off a validated reply while holding its transaction.
        if validate:
            for reply in results:
                self._refresh_reply(reply)
        return results

    def publish_reply(self, reply: dict, publish: Callable, *, end_dialogue=None) -> Optional[dict]:
        """Check again immediately before publishing while stores are open."""
        self._refresh_reply(reply)
        with self._condition:
            ticket = getattr(reply, '_delivery_ticket', None)
            if ticket is not None and ticket['state'] != 'waiting':
                return None
            if (self._closing or self._stopped or self._suspended
                    or getattr(reply, '_generation', self._generation)
                    != self._generation):
                self._release_prefetch(reply)
                return None
            if reply.get('kind') == 'addressee':
                return None
            if reply['kind'] == 'end_conversation':
                return dict(reply) if end_dialogue and end_dialogue(reply) else None
            if reply.get('kind') in {'progress', 'acknowledgement'}:
                return dict(reply) if reply._progress.publish(publish, reply['text']) else None
            dispatch = getattr(reply, '_mission_callback', None)
            if dispatch is not None:
                # Consume before calling transport, even if publication fails.
                reply._mission_callback = None
                try:
                    reply['text'] = dispatch()
                except Exception:
                    reply['text'] = '실행 요청의 처리 여부를 확인하지 못했어요. 자동으로 다시 보내지 않을게요.'
                    reply['kind'] = 'error'
            prefetch = getattr(reply, '_prefetch', None)
            if prefetch is not None:
                reply._prepared_hint = prefetch.hint(reply['text']) if reply['kind'] == 'answer' else {}
                if not reply._prepared_hint:
                    prefetch.cancel()
            published = False
            try:
                if ticket is not None:
                    reply['text'] = ticket['progress'].final_text(reply['text'])
                published = publish(reply['text'])
            finally:
                self._release_prefetch(reply, published=published)
                if ticket is not None:
                    ticket['state'] = 'published' if published else 'cancelled'
                    self._condition.notify_all()
            if published:
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
            self._mapping_name_question = None
            self._generation += 1
            self._cancel_prefetch()
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
            self._release_prefetch(reply)
            reply['text'] = MEMORY_CHANGED_RESPONSE
            reply['kind'] = 'error'
            reply._memory_validator = None
            reply._mission_callback = None
            reply._on_publish = None
        except Exception:
            self._release_prefetch(reply)
            reply['text'] = ERROR_RESPONSE
            reply['kind'] = 'error'
            reply._memory_validator = None
            reply._mission_callback = None
            reply._on_publish = None

    def close(self) -> None:
        """Discard waiting and late replies; let the running call finish."""
        self._companion.end()
        with self._condition:
            self._closing = True
            self._cancel_prefetch()
            self._navigation_confirmation = None
            self._mapping_name_question = None
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
                self._companion.service = getattr(runtime, 'companion_service', None)
                if self._companion.service:
                    self._companion.service.on_change = self._companion.clear_history
                    self._companion.service.start()
                start_memory = getattr(runtime, 'start_background_memory', None)
                if start_memory is not None:
                    start_memory()
                session = runtime.conversation_store.resume_or_create(self._user_id)
                conversation_id = session.conversation_id
                with self._condition:
                    self.patrol_memory = getattr(runtime, 'patrol_memory', None)
                    self._ready = True
                self._notify_reader()
            except Exception as error:
                with self._condition:
                    self._startup_error = type(error).__name__
                    self._stopped = True
                    self._notify_reader()
                    if not self._closing:
                        while self._pending:
                            utterance_id, _text, playback_id, progress = self._pending.popleft()
                            if progress is not None:
                                progress.finish(cancelled=True)
                            if playback_id is not None:
                                self._emit(self._addressee_reply(
                                    utterance_id, playback_id, 'unknown',
                                ))
                            else:
                                self._emit(self._reply(
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
                    mapping_question = None
                    if playback_id is None:
                        confirmation = self._navigation_confirmation
                        self._navigation_confirmation = None
                        mapping_question = self._mapping_name_question
                        self._mapping_name_question = None
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
                            self._emit(reply)
                    continue
                if getattr(progress, 'companion_session', None) is not None:
                    self._answer_companion(runtime, utterance_id, text, progress, generation)
                    continue
                prefetch = None
                delivery = {'published': False}
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
                    mapping_question = self._valid_mapping_name_question(session, mapping_question)
                    fresh_request = not runtime.conversation_store.has_agent_request(
                        self._user_id, request_id,
                    )
                    locations = (self._missions.navigation_locations()
                                 if self._missions is not None else None)
                    if (confirmation is not None
                            and confirmation.proposal.location not in (locations or ())):
                        confirmation = None
                    navigation_binding = (self._missions.prepare(
                        request_id, 'request_navigation', {'location': locations[0]},
                    ) if locations else None)
                    scene, image = None, None
                    if self._scene_source is not None:
                        try:
                            prepare = getattr(self._scene_source, 'prepare_turn',
                                              self._scene_source.snapshot)
                            scene, image = prepare()
                        except Exception:
                            scene = {'image': {'available': False}, 'map': None, 'pose': None}
                    opener = getattr(progress, 'opener_context', None)
                    if opener is not None and not start_new:
                        scene = dict(scene or {}, conversation_opener=opener)
                    request = SpeechAgentRequest(
                        request_id=request_id,
                        user_id=self._user_id,
                        conversation_id=conversation_id,
                        turn_id='speech-turn-' + digest,
                        utterance=text,
                        robot_state=RobotState(camera_available=image is not None),
                        scene_context=scene, turn_image=image,
                        available_tools=tuple(getattr(runtime, 'speech_mission_tools', ()))
                        + tuple(getattr(runtime, 'patrol_query_tools', ()))
                        + tuple(getattr(runtime, 'homecam_query_tools', ())) + (
                            ('get_weather', 'set_weather_location')
                            if getattr(runtime, 'weather_executor', None)
                            is not None else ()
                        ),
                        navigation_locations=locations,
                        navigation_confirmation=(confirmation.proposal.location
                                                 if confirmation is not None else ''),
                        mapping_name_requested=mapping_question is not None,
                    )
                    if self._prefetch_enabled:
                        from malbut_agent_server.speech_prefetch import PrefetchTurn

                        with self._condition:
                            prefetch = PrefetchTurn(
                                request_id, utterance_id, self._queue_preparation,
                                lock=self._condition,
                                current=lambda g=generation, epoch=progress.prefetch_epoch: (
                                    not self._closing and not self._suspended
                                    and g == self._generation and epoch == self._prefetch_epoch),
                            )
                            self._prefetch_turns[request_id] = prefetch
                    def deliver(result):
                        published = self._deliver_before_save(
                            result, utterance_id, conversation_id, generation, progress, prefetch,
                        )
                        delivery['published'] = published
                        return published

                    result = self._handle_with_progress(
                        runtime, request, progress, prefetch,
                        deliver if self._publish_before_save else None,
                    )
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
                            mapping_question=mapping_question,
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
                            message, 'end_conversation' if decision.ends_conversation else 'answer',
                            getattr(result, 'memory_validator', None),
                        )
                        if (self._missions is not None
                                and result.safety.code == 'navigation_confirmation_required'):
                            self._prepare_navigation_confirmation(runtime, request, result, reply)
                        elif (fresh_request and 'request_mapping' in request.available_tools
                              and decision.type == 'clarification'
                              and decision.clarification_for == 'mapping_name'):
                            self._prepare_mapping_name_question(runtime, request, result, reply)
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
                    if delivery['published']:
                        import logging
                        logging.getLogger(__name__).exception(
                            'speech_result_save_failed utterance_id=%s', utterance_id,
                        )
                    reply = self._reply(
                        utterance_id, conversation_id, ERROR_RESPONSE, 'error',
                    )
                finally:
                    if delivery['published']:
                        # A storage failure must not send a second spoken answer.
                        reply = None
                    if prefetch is not None and not delivery['published']:
                        if reply is None or reply['kind'] != 'answer':
                            prefetch.cancel()
                            with self._condition:
                                self._prefetch_turns.pop(prefetch.request_id, None)
                        else:
                            reply._prefetch = prefetch
                    progress.finish(cancelled=reply is None or reply['kind'] == 'end_conversation')
                with self._condition:
                    self._active_progress = None
                    self._running = False
                    if not self._closing:
                        if delivery['published']:
                            self._outstanding -= 1
                        elif reply is None or generation != self._generation:
                            if reply is not None:
                                self._release_prefetch(reply)
                            self._outstanding -= 1
                        else:
                            reply._generation = generation
                            from malbut_agent_server.mission_audio import TEXT_IDS
                            if reply['kind'] != 'end_conversation' and reply['text'] not in TEXT_IDS:
                                reply['text'] = progress.final_text(reply['text'])
                            self._emit(reply)
        finally:
            with self._condition:
                self._stopped = True
            if runtime is not None:
                if self._companion.service:
                    self._companion.service.close()
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

    def _valid_mapping_name_question(self, session, pending):
        """Consume a question only in its current, unexpired conversation turn."""
        if (pending is not None and time.monotonic() < pending.expires_at
                and session.status == 'active'
                and session.conversation_id == pending.conversation_id
                and session.generation == pending.generation
                and session.revision == pending.revision):
            return pending
        return None

    def _prepare_mapping_name_question(self, runtime, request, result, reply):
        """Arm a typed model question only once its committed reply is published."""
        def activate():
            expires_at = time.monotonic() + MAPPING_NAME_SECONDS
            if self._running or self._pending:
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
                    and time.monotonic() < expires_at):
                self._mapping_name_question = _MappingNameQuestion(
                    request.conversation_id, session.generation, session.revision, expires_at,
                )

        reply._on_publish = activate

    def _mission_reply(self, runtime, request, result, utterance_id, conversation_id,
                       *, confirmation=None, navigation_binding=None, mapping_question=None):
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
            if decision.tool_name in {'request_navigation_point', 'request_waypoints'}:
                revision = ((request.scene_context or {}).get('map') or {}).get('revision')
                if revision is None or not self._missions.map_matches(revision):
                    return NAVIGATION_CHANGED_RESPONSE
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
            if (mapping_question is not None and decision.tool_name == 'request_mapping'
                    and time.monotonic() >= mapping_question.expires_at):
                return '요청의 유효 시간이 지나 실행하지 않았어요. 다시 말씀해 주세요.'
            return None

        reply._mission_callback = lambda: self._missions.dispatch(proposal, guard=guard)
        reply._mission_request_id = proposal.manager_request_id
        return reply

    def _progress_notice(self, utterance_id, text, progress):
        with self._condition:
            if self._closing or not progress.active:
                return
            kind = 'acknowledgement' if text in FUNCTION_STARTS.values() else 'progress'
            reply = self._reply(utterance_id, None, text, kind)
            reply._progress = progress
            reply._generation = self._generation
            self._emit(reply)

    @staticmethod
    def _handle_with_progress(runtime, request, progress, prefetch=None, delivery=None):
        from malbut_agent_server.speech_prefetch import prefetch_scope
        from malbut_agent_server.speech_delivery import delivery_scope

        with request_scope(progress=progress), prefetch_scope(prefetch), delivery_scope(delivery):
            return runtime.handle(request)

    def _deliver_before_save(self, result, utterance_id, conversation_id,
                             generation, progress, prefetch):
        """Let the ROS thread publish within the worker's validation transaction.

        No DB guard runs on the ROS thread for this ticket: the worker has just
        checked final memory and holds SQLite's writer lock until we return.
        A bounded wait and atomic ticket expiry avoid deadlock with other ROS
        callbacks using the database. Late tickets can never publish twice.
        """
        if result.decision.ends_conversation:
            # The committed result sends a turn control instead of a speech ticket.
            return False
        deadline = time.monotonic() + 0.25
        if not self._condition.acquire(timeout=0.25):
            return False
        try:
            if (self._closing or self._suspended or generation != self._generation):
                return False
            text = result.decision.message
            from malbut_agent_server.mission_audio import CATALOG
            if ((result.decision.type == 'refusal' and result.decision.reason in {
                    'safety:unknown_tool', 'safety:tool_unavailable',
            }) or result.decision.reason in {'unsupported_capability', 'unavailable_capability'}):
                text = CATALOG['operation.unsupported']
            reply = self._reply(utterance_id, conversation_id, text, 'answer')
            reply['delivery_before_save'] = True
            ticket = reply._delivery_ticket = {'state': 'waiting', 'progress': progress}
            reply._generation = generation
            reply._prefetch = prefetch
            self._emit(reply)
            self._condition.wait_for(
                lambda: ticket['state'] != 'waiting' or self._closing
                or self._suspended or generation != self._generation,
                timeout=max(0, deadline - time.monotonic()),
            )
            if ticket['state'] == 'published':
                progress.finish()
                return True
            ticket['state'] = 'cancelled'
            # The ROS thread may already have drained it. The cancelled ticket
            # also guards that copy, so only the normal post-save reply remains.
            self._results = deque(item for item in self._results if item is not reply)
            return False
        finally:
            self._condition.release()

    def begin_companion(self, session):
        """Expose the encounter to the app before the first spoken reply."""
        self._companion.begin(session)

    def end_companion(self, session):
        """Discard an encounter, including a model response still in flight."""
        self._companion.end(session)

    def companion_reply_allowed(self, session):
        return self._companion.reply_allowed(session)

    def _answer_companion(self, runtime, utterance_id, text, progress, generation):
        from malbut_agent_server.companion_dialogue import CompanionOutcome

        session = progress.companion_session
        try:
            with request_scope(progress=progress):
                provider = getattr(runtime, 'companion_provider', runtime.provider)
                message = self._companion.answer(provider, session, utterance_id,
                                                 text, progress.opener_context,
                                                 scene_source=self._scene_source)
        except Exception:
            message = ERROR_RESPONSE
        receipt = self._companion.reply_receipt()
        ended = message is CompanionOutcome.END_CONVERSATION
        progress.finish(cancelled=message is None or ended)
        with self._condition:
            self._active_progress = None
            self._running = False
            if not self._closing:
                if message is None or generation != self._generation:
                    self._outstanding -= 1
                else:
                    reply = self._reply(utterance_id, session, '' if ended else message,
                                        'end_conversation' if ended else 'answer')
                    reply._companion_session = session
                    def validate():
                        if not self._companion.reply_allowed(session, receipt):
                            raise MemoryChangedError('companion identity or memory changed')
                    reply._memory_validator = validate
                    reply._generation = generation
                    self._emit(reply)

    @staticmethod
    def _addressee_reply(utterance_id, playback_id, decision):
        return {
            'kind': 'addressee', 'utterance_id': utterance_id,
            'playback_id': playback_id, 'decision': decision,
        }

    def _classify_interruption(self, runtime, conversation_id, utterance_id, playback_id, text):
        try:
            snapshot = self._companion.snapshot()
            if snapshot is None:
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
