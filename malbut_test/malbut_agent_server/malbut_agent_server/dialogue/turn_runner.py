"""Off-thread dialogue worker loop decomposed into focused turn stages."""

from concurrent.futures import CancelledError
from contextlib import nullcontext as _nullcontext
import hashlib
import logging
from typing import Any, Dict, Optional, Tuple

from malbut_agent_server.conversation import (
    ConversationNotFoundError,
    ConversationStateError,
)
from malbut_agent_server.dialogue.models import (
    ERROR_RESPONSE,
    MEMORY_CHANGED_RESPONSE,
    NAVIGATION_CHANGED_RESPONSE,
    SESSION_ERROR_RESPONSE,
    addressee_reply,
    make_reply,
    starts_new_conversation,
)
from malbut_agent_server.orchestrator import MemoryChangedError
from malbut_agent_server.schemas import RobotState, SpeechAgentRequest


def _start_runtime(worker: Any, runtime: Any) -> Optional[str]:
    if hasattr(runtime, 'turn_metrics'):
        runtime.turn_metrics = worker.turn_metrics
    worker._companion.service = getattr(runtime, 'companion_service', None)
    if worker._companion.service:
        worker._companion.service.on_change = worker._companion.clear_history
        worker._companion.service.start()
    start_memory = getattr(runtime, 'start_background_memory', None)
    if start_memory is not None:
        start_memory()
    session = runtime.conversation_store.resume_or_create(worker._user_id)
    conversation_id = session.conversation_id
    with worker._condition:
        worker.patrol_memory = getattr(runtime, 'patrol_memory', None)
        worker._ready = True
    worker._notify_reader()
    return conversation_id


def _handle_startup_failure(
    worker: Any,
    error: Exception,
    conversation_id: Optional[str],
) -> None:
    failed_traces = []
    with worker._condition:
        worker._startup_error = type(error).__name__
        worker._stopped = True
        worker._notify_reader()
        if not worker._closing:
            while worker._pending:
                utterance_id, _text, playback_id, progress = worker._pending.popleft()
                trace = (
                    worker._pending_traces.pop(id(progress), None)
                    if progress is not None else None
                )
                if progress is not None:
                    progress.finish(cancelled=True)
                if playback_id is not None:
                    worker._emit(addressee_reply(utterance_id, playback_id, 'unknown'))
                else:
                    if trace is not None:
                        failed_traces.append(trace)
                    worker._emit(make_reply(
                        utterance_id, conversation_id, ERROR_RESPONSE, 'error',
                    ))
    for trace in failed_traces:
        trace.finalize(worker.turn_metrics, outcome='error')


def _handle_interruption(
    worker: Any,
    runtime: Any,
    conversation_id: Optional[str],
    utterance_id: str,
    playback_id: str,
    text: str,
    generation: int,
) -> None:
    reply = worker._classify_interruption(
        runtime, conversation_id, utterance_id, playback_id, text,
    )
    with worker._condition:
        worker._running = False
        if not worker._closing and generation != worker._generation:
            worker._outstanding -= 1
        elif not worker._closing:
            entry = worker._interruptions[utterance_id]
            entry['pending'] = False
            if entry['conflict']:
                reply['decision'] = 'unknown'
            entry['decision'] = reply['decision']
            worker._emit(reply)


def _prepare_turn_scene(
    worker: Any,
    request_id: str,
    text: str,
    confirmation: Any,
    mapping_question: Any,
    trace: Any,
) -> Tuple[Any, Any, Any, Any, Any]:
    scene, image = None, None
    with (trace.measure('scene_prepare_ms') if trace is not None else _nullcontext()):
        locations = (
            worker._missions.navigation_locations()
            if worker._missions is not None else None
        )
        if confirmation is not None and confirmation.proposal.location not in (locations or ()):
            confirmation = None
        navigation_binding = (
            worker._missions.prepare(
                request_id, 'request_navigation', {'location': locations[0]},
            )
            if locations else None
        )
        if worker._scene_source is not None:
            try:
                from malbut_agent_server.speech_scene import needs_turn_image

                want_image = needs_turn_image(
                    text,
                    navigation_locations=locations or (),
                    navigation_confirmation=(
                        confirmation.proposal.location if confirmation is not None else ''
                    ),
                    mapping_name_requested=mapping_question is not None,
                )
                if not want_image and hasattr(worker._scene_source, 'prepare_text_turn'):
                    scene, image = worker._scene_source.prepare_text_turn()
                else:
                    prepare = getattr(worker._scene_source, 'prepare_turn', None)
                    if prepare is None:
                        prepare = worker._scene_source.snapshot
                    scene, image = prepare()
            except Exception:
                scene = {'image': {'available': False}, 'map': None, 'pose': None}
    return locations, confirmation, navigation_binding, scene, image


def _build_speech_request(
    worker: Any,
    runtime: Any,
    *,
    request_id: str,
    conversation_id: str,
    digest: str,
    text: str,
    scene: Any,
    image: Any,
    locations: Any,
    confirmation: Any,
    mapping_question: Any,
) -> SpeechAgentRequest:
    return SpeechAgentRequest(
        request_id=request_id,
        user_id=worker._user_id,
        conversation_id=conversation_id,
        turn_id='speech-turn-' + digest,
        utterance=text,
        robot_state=RobotState(camera_available=image is not None),
        scene_context=scene,
        turn_image=image,
        available_tools=(
            tuple(getattr(runtime, 'speech_mission_tools', ()))
            + tuple(getattr(runtime, 'patrol_query_tools', ()))
            + tuple(getattr(runtime, 'homecam_query_tools', ()))
            + (('get_weather', 'set_weather_location')
               if getattr(runtime, 'weather_executor', None) is not None else ())
        ),
        navigation_locations=locations,
        navigation_confirmation=(
            confirmation.proposal.location if confirmation is not None else ''
        ),
        mapping_name_requested=mapping_question is not None,
    )


def _format_turn_reply(
    worker: Any,
    runtime: Any,
    request: SpeechAgentRequest,
    result: Any,
    *,
    utterance_id: str,
    conversation_id: str,
    fresh_request: bool,
    confirmation: Any,
    navigation_binding: Any,
    mapping_question: Any,
) -> Any:
    decision = result.decision
    if (navigation_binding is not None
            and result.raw_decision.tool_name == 'request_navigation'
            and not worker._missions.navigation_matches(navigation_binding)):
        return make_reply(utterance_id, conversation_id, NAVIGATION_CHANGED_RESPONSE, 'answer')
    if decision.type == 'tool_call' and worker._missions is not None:
        return worker._mission_reply(
            runtime, request, result, utterance_id, conversation_id,
            confirmation=confirmation, navigation_binding=navigation_binding,
            mapping_question=mapping_question,
        )
    if (decision.type not in {'message', 'clarification', 'refusal'}
            or not isinstance(decision.message, str)
            or not decision.message.strip()):
        raise ValueError('Expected a non-action response')
    message = decision.message
    if ((decision.type == 'refusal' and decision.reason in {
            'safety:unknown_tool', 'safety:tool_unavailable',
    }) or (decision.type == 'message' and decision.reason in {
            'unsupported_capability', 'unavailable_capability',
    })):
        from malbut_agent_server.mission_audio import CATALOG
        message = CATALOG['operation.unsupported']
    reply = make_reply(
        utterance_id,
        conversation_id,
        message,
        'end_conversation' if decision.ends_conversation else 'answer',
        getattr(result, 'memory_validator', None),
    )
    if worker._missions is not None and result.safety.code == 'navigation_confirmation_required':
        worker._prepare_navigation_confirmation(runtime, request, result, reply)
    elif (fresh_request and 'request_mapping' in request.available_tools
          and decision.type == 'clarification'
          and decision.clarification_for == 'mapping_name'):
        worker._prepare_mapping_name_question(runtime, request, result, reply)
    return reply


def _execute_dialogue_turn(
    worker: Any,
    runtime: Any,
    conversation_id: Optional[str],
    *,
    utterance_id: str,
    text: str,
    progress: Any,
    generation: int,
    confirmation: Any,
    mapping_question: Any,
    trace: Any,
) -> Optional[str]:
    prefetch = None
    delivery: Dict[str, bool] = {'published': False}
    turn_outcome = 'ok'
    reply = None
    try:
        digest = hashlib.sha256(utterance_id.encode('utf-8')).hexdigest()
        request_id = 'speech-request-' + digest
        start_new = (
            starts_new_conversation(text)
            and not runtime.conversation_store.has_agent_request(worker._user_id, request_id)
        )
        session = runtime.conversation_store.resume_or_create(
            worker._user_id, conversation_id, start_new=start_new,
        )
        conversation_id = session.conversation_id
        assert conversation_id is not None
        if trace is not None:
            trace.set_metadata(request_id=request_id, conversation_id=conversation_id)
        confirmation = worker._valid_navigation_confirmation(session, confirmation)
        mapping_question = worker._valid_mapping_name_question(session, mapping_question)
        fresh_request = not runtime.conversation_store.has_agent_request(
            worker._user_id, request_id,
        )
        locations, confirmation, navigation_binding, scene, image = _prepare_turn_scene(
            worker, request_id, text, confirmation, mapping_question, trace,
        )
        if trace is not None:
            trace.set_flag('image_captured', image is not None)
        opener = getattr(progress, 'opener_context', None)
        if opener is not None and not start_new:
            scene = dict(scene or {}, conversation_opener=opener)
        request = _build_speech_request(
            worker, runtime,
            request_id=request_id,
            conversation_id=conversation_id,
            digest=digest,
            text=text,
            scene=scene,
            image=image,
            locations=locations,
            confirmation=confirmation,
            mapping_question=mapping_question,
        )
        if worker._prefetch_enabled:
            from malbut_agent_server.speech_prefetch import PrefetchTurn

            with worker._condition:
                prefetch = PrefetchTurn(
                    request_id, utterance_id, worker._queue_preparation,
                    lock=worker._condition,
                    current=lambda g=generation, epoch=progress.prefetch_epoch: (
                        not worker._closing and not worker._suspended
                        and g == worker._generation and epoch == worker._prefetch_epoch
                    ),
                )
                worker._prefetch_turns[request_id] = prefetch

        def deliver(result: Any) -> bool:
            published = worker._deliver_before_save(
                result, utterance_id, conversation_id,
                generation, progress, prefetch, trace=trace,
            )
            delivery['published'] = published
            return published

        result = worker._handle_with_progress(
            runtime, request, progress, prefetch,
            deliver if worker._publish_before_save else None,
            trace=trace,
        )
        reply = _format_turn_reply(
            worker, runtime, request, result,
            utterance_id=utterance_id,
            conversation_id=conversation_id,
            fresh_request=fresh_request,
            confirmation=confirmation,
            navigation_binding=navigation_binding,
            mapping_question=mapping_question,
        )
    except CancelledError:
        turn_outcome = 'cancelled'
        reply = None
    except (ConversationNotFoundError, ConversationStateError):
        turn_outcome = 'error'
        reply = make_reply(utterance_id, conversation_id, SESSION_ERROR_RESPONSE, 'error')
    except MemoryChangedError:
        turn_outcome = 'error'
        reply = make_reply(utterance_id, conversation_id, MEMORY_CHANGED_RESPONSE, 'error')
    except Exception:
        turn_outcome = 'error'
        if delivery['published']:
            logging.getLogger(__name__).exception(
                'speech_result_save_failed utterance_id=%s', utterance_id,
            )
        reply = make_reply(utterance_id, conversation_id, ERROR_RESPONSE, 'error')
    finally:
        if delivery['published']:
            # A storage failure must not send a second spoken answer.
            reply = None
        if prefetch is not None and not delivery['published']:
            if reply is None or reply['kind'] != 'answer':
                prefetch.cancel()
                with worker._condition:
                    worker._prefetch_turns.pop(prefetch.request_id, None)
            else:
                reply._prefetch = prefetch
        progress.finish(cancelled=reply is None or reply['kind'] == 'end_conversation')
    _finalize_dialogue_turn(
        worker,
        reply=reply,
        progress=progress,
        generation=generation,
        delivery_published=delivery['published'],
        turn_outcome=turn_outcome,
        trace=trace,
    )
    return conversation_id


def _finalize_dialogue_turn(
    worker: Any,
    *,
    reply: Any,
    progress: Any,
    generation: int,
    delivery_published: bool,
    turn_outcome: str,
    trace: Any,
) -> None:
    finalize_outcome = None
    with worker._condition:
        worker._active_progress = None
        worker._running = False
        if worker._closing:
            if trace is not None and not trace.finalized:
                finalize_outcome = turn_outcome if delivery_published else 'cancelled'
        elif delivery_published:
            worker._outstanding -= 1
            if trace is not None and not trace.finalized:
                finalize_outcome = turn_outcome
        elif reply is None or generation != worker._generation:
            if reply is not None:
                worker._release_prefetch(reply)
            worker._outstanding -= 1
            if trace is not None and not trace.finalized:
                finalize_outcome = 'cancelled'
        else:
            reply._generation = generation
            if trace is not None:
                if turn_outcome == 'error':
                    trace.set_metadata(outcome='error', decision_type='error')
                trace.mark_emitted()
                reply._turn_trace = trace
            from malbut_agent_server.mission_audio import TEXT_IDS
            if reply['kind'] != 'end_conversation' and reply['text'] not in TEXT_IDS:
                reply['text'] = progress.final_text(reply['text'])
            worker._emit(reply)
    if trace is not None and finalize_outcome is not None:
        trace.finalize(worker.turn_metrics, outcome=finalize_outcome)


def _shutdown_runtime(worker: Any, runtime: Any) -> None:
    with worker._condition:
        worker._stopped = True
    if runtime is None:
        return
    if worker._companion.service:
        worker._companion.service.close()
    close_runtime = getattr(runtime, 'close', None)
    if close_runtime is not None:
        close_runtime()
    else:
        # Retain the existing injected-runtime adapter contract.
        try:
            runtime.conversation_store.close()
        finally:
            runtime.memory_store.close()


def run_worker_loop(worker: Any) -> None:
    """Main worker-thread loop for DialogueWorker."""
    runtime = None
    conversation_id: Optional[str] = None
    try:
        try:
            runtime = worker._factory()
            conversation_id = _start_runtime(worker, runtime)
        except Exception as error:
            _handle_startup_failure(worker, error, conversation_id)
            return
        while True:
            with worker._condition:
                worker._condition.wait_for(
                    lambda: worker._closing or bool(worker._pending),
                )
                if worker._closing:
                    return
                utterance_id, text, playback_id, progress = worker._pending.popleft()
                trace = (
                    worker._pending_traces.pop(id(progress), None)
                    if progress is not None else None
                )
                confirmation = None
                mapping_question = None
                if playback_id is None:
                    confirmation = worker._navigation_confirmation
                    worker._navigation_confirmation = None
                    mapping_question = worker._mapping_name_question
                    worker._mapping_name_question = None
                generation = worker._generation
                worker._running = True
                worker._active_progress = progress
            if playback_id is not None:
                _handle_interruption(
                    worker, runtime, conversation_id,
                    utterance_id, playback_id, text, generation,
                )
                continue
            if trace is not None:
                trace.mark_dequeued()
            if getattr(progress, 'companion_session', None) is not None:
                worker._answer_companion(
                    runtime, utterance_id, text, progress, generation, trace=trace,
                )
                continue
            conversation_id = _execute_dialogue_turn(
                worker, runtime, conversation_id,
                utterance_id=utterance_id,
                text=text,
                progress=progress,
                generation=generation,
                confirmation=confirmation,
                mapping_question=mapping_question,
                trace=trace,
            )
    finally:
        _shutdown_runtime(worker, runtime)
