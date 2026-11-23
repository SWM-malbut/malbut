"""Run existing conversation handling outside the ROS callback thread."""

from collections import OrderedDict, deque
import contextlib
import hashlib
import math
import sqlite3
from threading import Condition, Thread
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from malbut_agent_server.conversation import (
    ConversationNotFoundError,
    ConversationStateError,
)
from malbut_agent_server.conversation_progress import RequestProgress, request_scope
from malbut_agent_server.dialogue import (
    ADDRESSEE_DECISIONS,
    ERROR_RESPONSE,
    MAPPING_NAME_SECONDS,
    MAX_INTERRUPTION_IDS,
    MAX_SPEECH_ID_LENGTH,
    MEMORY_CHANGED_RESPONSE,
    NAVIGATION_CHANGED_RESPONSE,
    NAVIGATION_CONFIRMATION_SECONDS,
    NEW_CONVERSATION_REQUESTS,
    SESSION_ERROR_RESPONSE,
    SpeechInputTooLongError,
    _DialogueReply,
    _MappingNameQuestion,
    _NavigationConfirmation,
    addressee_reply,
    answer_companion,
    classify_interruption,
    deliver_before_save,
    make_reply,
    publish_reply,
    refresh_reply,
    run_worker_loop,
    starts_new_conversation,
    validate_dialogue_input,
    validate_interruption_input,
)
from malbut_agent_server.function_speech import FUNCTION_STARTS
from malbut_agent_server.schemas import validate_user_id
from malbut_agent_server.turn_metrics import (
    TurnWaterfallRecorder,
    TurnWaterfallTrace,
    turn_trace_scope,
)

__all__ = [
    'ADDRESSEE_DECISIONS',
    'DialogueWorker',
    'ERROR_RESPONSE',
    'MAPPING_NAME_SECONDS',
    'MAX_INTERRUPTION_IDS',
    'MAX_SPEECH_ID_LENGTH',
    'MEMORY_CHANGED_RESPONSE',
    'NAVIGATION_CHANGED_RESPONSE',
    'NAVIGATION_CONFIRMATION_SECONDS',
    'NEW_CONVERSATION_REQUESTS',
    'SESSION_ERROR_RESPONSE',
    'SpeechInputTooLongError',
    '_DialogueReply',
    '_MappingNameQuestion',
    '_NavigationConfirmation',
    'starts_new_conversation',
    'validate_dialogue_input',
    'validate_interruption_input',
]


class DialogueWorker:
    """Keep one conversation ordered and bound queued and unread work.

    ``runtime_factory`` constructs the existing AgentOrchestrator in this
    worker. Its stores are closed here after the final running call returns.
    The ROS thread retains ownership of receipt storage and text publication.
    """

    def __init__(
        self,
        runtime_factory: Callable[[], Any],
        user_id: str,
        capacity: int = 10,
        *,
        missions: Any = None,
        scene_source: Any = None,
        prefetch_enabled: bool = False,
        publish_before_save: bool = False,
        on_output: Optional[Callable[[], Any]] = None,
        turn_metrics: Optional[TurnWaterfallRecorder] = None,
    ) -> None:
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
        self._preparations: deque[Any] = deque()
        self._prefetch_turns: Dict[str, Any] = {}
        self._prefetch_epoch = 0
        self.patrol_memory: Any = None
        from malbut_agent_server.companion_dialogue import CompanionDialogue
        self._companion = CompanionDialogue()
        self._navigation_confirmation: Optional[_NavigationConfirmation] = None
        self._mapping_name_question: Optional[_MappingNameQuestion] = None
        self._user_id = validate_user_id(user_id)
        self._capacity = capacity
        self._condition = Condition()
        self._pending: deque[Tuple[str, str, Optional[str], Any]] = deque()
        self._results: deque[Any] = deque()
        self._interruptions: 'OrderedDict[str, Dict[str, Any]]' = OrderedDict()
        self.turn_metrics = turn_metrics or TurnWaterfallRecorder()
        self._pending_traces: Dict[int, TurnWaterfallTrace] = {}
        self._outstanding = 0
        self._closing = False
        self._stopped = False
        self._ready = False
        self._startup_error: Optional[str] = None
        self._active_progress: Any = None
        self._suspended = False
        self._generation = 0
        self._running = False
        self._thread = Thread(target=self._run, name='malbut-speech-dialogue')
        self._thread.start()

    def _notify_reader(self) -> None:
        # Optional ROS wakeup only; the owning thread still publishes and saves.
        if self._on_output is not None:
            with contextlib.suppress(Exception):
                self._on_output()

    def _emit(self, reply: Any) -> None:
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

    def metrics_snapshot(self, limit: int = 20) -> Dict[str, Any]:
        """Return content-free rolling latency summary and recent speech turns."""
        return self.turn_metrics.snapshot(limit=limit)

    def submit(
        self,
        utterance_id: str,
        text: str,
        *,
        opener_context: Any = None,
        companion_session: Any = None,
    ) -> bool:
        """Queue an original utterance without waiting for model inference."""
        validate_dialogue_input(utterance_id, text)
        with self._condition:
            if not self.has_capacity():
                return False
            self._cancel_prefetch()
            self._outstanding += 1
            progress: Any = RequestProgress(
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
            self._pending_traces[id(progress)] = TurnWaterfallTrace(
                channel='speech',
                utterance_id=utterance_id,
            )
            self._pending.append((utterance_id, text, None, progress))
            self._condition.notify()
            return True

    def submit_interruption(self, utterance_id: str, playback_id: str, text: str) -> bool:
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
                expired = next((
                    uid for uid, entry in self._interruptions.items()
                    if not entry['pending']
                ), None)
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

    def _cancel_prefetch(self) -> None:
        self._prefetch_epoch += 1
        for turn in tuple(self._prefetch_turns.values()):
            turn.cancel()
        self._prefetch_turns.clear()

    def _queue_preparation(self, event: Any) -> bool:
        # The ROS thread publishes these separately from speech replies. A
        # dropped optimization must not prevent ordinary final text delivery.
        with self._condition:
            if event.operation == 'cancel':
                self._preparations = deque(
                    item for item in self._preparations
                    if item.prepared_id != event.prepared_id
                )
            elif self._closing or self._suspended:
                return False
            if len(self._preparations) >= 32:
                if event.operation != 'cancel':
                    return False
                self._preparations.popleft()
            self._preparations.append(event)
            self._notify_reader()
            return True

    def drain_preparations(self) -> List[Any]:
        """Return quiet preparation commands, never playback status or replies."""
        with self._condition:
            events = list(self._preparations)
            self._preparations.clear()
            return events

    def cancel_preparations(self) -> None:
        """Invalidate quiet work before waiting for an in-flight request to end."""
        with self._condition:
            self._cancel_prefetch()

    def discard_reply(self, reply: Any) -> None:
        """Release quiet work when the ROS publication policy suppresses a reply."""
        with self._condition:
            self._release_prefetch(reply)
        trace = getattr(reply, '_turn_trace', None)
        if trace is not None and not trace.finalized:
            trace.finalize(self.turn_metrics, outcome='cancelled')

    def _release_prefetch(self, reply: Any, *, published: bool = False) -> None:
        turn = getattr(reply, '_prefetch', None)
        if turn is not None:
            if not published:
                turn.cancel()
            self._prefetch_turns.pop(turn.request_id, None)

    def drain(self, *, validate: bool = True) -> List[Dict[str, Any]]:
        """Recheck ready replies and release their admission capacity."""
        with self._condition:
            results = list(self._results)
            self._results.clear()
            self._outstanding -= sum(
                item['kind'] not in {'progress', 'acknowledgement'}
                and getattr(item, '_delivery_ticket', None) is None
                for item in results
            )
            results = [
                item for item in results
                if item['kind'] not in {'progress', 'acknowledgement'}
                or item._progress.can_publish(item['text'])
            ]
        # Never acquire the DB lock while holding the queue lock: the worker
        # may be handing off a validated reply while holding its transaction.
        if validate:
            for reply in results:
                self._refresh_reply(reply)
        return results

    def publish_reply(
        self,
        reply: Dict[str, Any],
        publish: Callable[[str], bool],
        *,
        end_dialogue: Optional[Callable[[Any], Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Check again immediately before publishing while stores are open."""
        return publish_reply(self, reply, publish, end_dialogue=end_dialogue)

    def _cancel_traces(self, traces: List[Any]) -> None:
        for trace in traces:
            if trace is not None and not trace.finalized:
                trace.finalize(self.turn_metrics, outcome='cancelled')

    def suspend(self) -> None:
        """Preempt ordinary speech and invalidate queued or in-flight replies."""
        cancelled_traces = []
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
                    cancelled_traces.append(self._pending_traces.pop(id(progress), None))
            for item in self._results:
                if getattr(item, '_delivery_ticket', None) is None:
                    cancelled_traces.append(getattr(item, '_turn_trace', None))
            self._pending.clear()
            self._pending_traces.clear()
            self._results.clear()
            self._interruptions.clear()
            self._outstanding = int(self._running)
        self._cancel_traces(cancelled_traces)

    def resume(self) -> None:
        """Accept new ordinary turns without replaying preempted replies."""
        with self._condition:
            self._suspended = False

    def _refresh_reply(self, reply: Any) -> None:
        refresh_reply(self, reply)

    def close(self) -> None:
        """Discard waiting and late replies; let the running call finish."""
        self._companion.end()
        cancelled_traces = []
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
                    cancelled_traces.append(self._pending_traces.pop(id(progress), None))
            for item in self._results:
                if getattr(item, '_delivery_ticket', None) is None:
                    cancelled_traces.append(getattr(item, '_turn_trace', None))
            self._pending.clear()
            self._pending_traces.clear()
            self._results.clear()
            self._interruptions.clear()
            self._outstanding = 0
            self._condition.notify_all()
        self._cancel_traces(cancelled_traces)
        self._thread.join()

    def _run(self) -> None:
        run_worker_loop(self)

    def _valid_navigation_confirmation(
        self, session: Any, pending: Optional[_NavigationConfirmation],
    ) -> Optional[_NavigationConfirmation]:
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

    def _prepare_navigation_confirmation(
        self, runtime: Any, request: Any, result: Any, reply: _DialogueReply,
    ) -> None:
        """Bind the question to its target and activate only after publication."""
        if result.raw_decision.type != 'tool_call':
            # A cached clarification has no fresh proposal and cannot re-arm it.
            return
        proposal = self._missions.prepare(
            request.request_id, 'request_navigation', result.raw_decision.arguments,
        )

        def activate() -> None:
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

    def _valid_mapping_name_question(
        self, session: Any, pending: Optional[_MappingNameQuestion],
    ) -> Optional[_MappingNameQuestion]:
        """Consume a question only in its current, unexpired conversation turn."""
        if (pending is not None and time.monotonic() < pending.expires_at
                and session.status == 'active'
                and session.conversation_id == pending.conversation_id
                and session.generation == pending.generation
                and session.revision == pending.revision):
            return pending
        return None

    def _prepare_mapping_name_question(
        self, runtime: Any, request: Any, result: Any, reply: _DialogueReply,
    ) -> None:
        """Arm a typed model question only once its committed reply is published."""
        def activate() -> None:
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

    def _mission_reply(
        self,
        runtime: Any,
        request: Any,
        result: Any,
        utterance_id: str,
        conversation_id: str,
        *,
        confirmation: Optional[_NavigationConfirmation] = None,
        navigation_binding: Any = None,
        mapping_question: Optional[_MappingNameQuestion] = None,
    ) -> _DialogueReply:
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

        def guard() -> Optional[str]:
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

    def _progress_notice(self, utterance_id: str, text: str, progress: Any) -> None:
        with self._condition:
            if self._closing or not progress.active:
                return
            kind = 'acknowledgement' if text in FUNCTION_STARTS.values() else 'progress'
            reply = self._reply(utterance_id, None, text, kind)
            reply._progress = progress
            reply._generation = self._generation
            self._emit(reply)

    @staticmethod
    def _handle_with_progress(
        runtime: Any,
        request: Any,
        progress: Any,
        prefetch: Any = None,
        delivery: Any = None,
        *,
        trace: Any = None,
    ) -> Any:
        from malbut_agent_server.speech_delivery import delivery_scope
        from malbut_agent_server.speech_prefetch import prefetch_scope

        with (
            request_scope(progress=progress),
            prefetch_scope(prefetch),
            delivery_scope(delivery),
            turn_trace_scope(trace),
        ):
            return runtime.handle(request)

    def _deliver_before_save(
        self,
        result: Any,
        utterance_id: str,
        conversation_id: str,
        generation: int,
        progress: Any,
        prefetch: Any,
        *,
        trace: Any = None,
    ) -> bool:
        return deliver_before_save(
            self, result, utterance_id, conversation_id, generation,
            progress, prefetch, clock=time.monotonic, trace=trace,
        )

    def begin_companion(self, session: Any) -> None:
        """Expose the encounter to the app before the first spoken reply."""
        self._companion.begin(session)

    def end_companion(self, session: Any) -> None:
        """Discard an encounter, including a model response still in flight."""
        self._companion.end(session)

    def companion_reply_allowed(self, session: Any) -> bool:
        return self._companion.reply_allowed(session)

    def _answer_companion(
        self,
        runtime: Any,
        utterance_id: str,
        text: str,
        progress: Any,
        generation: int,
        *,
        trace: Any = None,
    ) -> None:
        answer_companion(
            self, runtime, utterance_id, text, progress, generation, trace=trace,
        )

    @staticmethod
    def _addressee_reply(
        utterance_id: str, playback_id: str, decision: str,
    ) -> Dict[str, str]:
        return addressee_reply(utterance_id, playback_id, decision)

    def _classify_interruption(
        self,
        runtime: Any,
        conversation_id: Optional[str],
        utterance_id: str,
        playback_id: str,
        text: str,
    ) -> Dict[str, str]:
        return classify_interruption(
            self, runtime, conversation_id, utterance_id, playback_id, text,
        )

    @staticmethod
    def _reply(
        utterance_id: str,
        conversation_id: Optional[str],
        text: str,
        kind: str,
        memory_validator: Optional[Callable[[], Any]] = None,
    ) -> _DialogueReply:
        return make_reply(utterance_id, conversation_id, text, kind, memory_validator)
