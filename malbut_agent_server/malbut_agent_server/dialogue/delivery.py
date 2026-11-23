"""Reply publication, pre-save delivery handoff, companion replies, and addressee."""

from collections import deque
from contextlib import nullcontext as _nullcontext
from typing import Any, Callable, Dict, Optional

from malbut_agent_server.conversation_progress import request_scope
from malbut_agent_server.dialogue.models import (
    ADDRESSEE_DECISIONS,
    ERROR_RESPONSE,
    MEMORY_CHANGED_RESPONSE,
    _DialogueReply,
    addressee_reply,
    make_reply,
)
from malbut_agent_server.orchestrator import MemoryChangedError


def refresh_reply(worker: Any, reply: Any) -> None:
    """Re-run a reply's memory validator, downgrading to an error notice on drift."""
    validator = getattr(reply, '_memory_validator', None)
    if validator is None:
        return
    try:
        if worker._closing or worker._stopped:
            raise MemoryChangedError('dialogue worker is closed')
        validator()
    except MemoryChangedError:
        worker._release_prefetch(reply)
        reply['text'] = MEMORY_CHANGED_RESPONSE
        reply['kind'] = 'error'
        reply._memory_validator = None
        reply._mission_callback = None
        reply._on_publish = None
    except Exception:
        worker._release_prefetch(reply)
        reply['text'] = ERROR_RESPONSE
        reply['kind'] = 'error'
        reply._memory_validator = None
        reply._mission_callback = None
        reply._on_publish = None


def publish_reply(
    worker: Any,
    reply: Any,
    publish: Callable[[str], bool],
    *,
    end_dialogue: Optional[Callable[[Any], Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Check again immediately before publishing while stores are open."""
    refresh_reply(worker, reply)
    with worker._condition:
        ticket = getattr(reply, '_delivery_ticket', None)
        trace = getattr(reply, '_turn_trace', None)
        if ticket is not None and ticket['state'] != 'waiting':
            return None
        if (worker._closing or worker._stopped or worker._suspended
                or getattr(reply, '_generation', worker._generation)
                != worker._generation):
            worker._release_prefetch(reply)
            if trace is not None and ticket is None and not trace.finalized:
                trace.finalize(worker.turn_metrics, outcome='cancelled')
            return None
        if reply.get('kind') == 'addressee':
            return None
        if reply['kind'] == 'end_conversation':
            ended = bool(end_dialogue and end_dialogue(reply))
            if trace is not None and not trace.finalized:
                trace.set_metadata(decision_type='end_conversation')
                record = trace.finalize(
                    worker.turn_metrics,
                    outcome='ok' if ended else 'cancelled',
                )
                if isinstance(reply, _DialogueReply):
                    reply._waterfall = record.to_compact_dict()
            return dict(reply) if ended else None
        if reply.get('kind') in {'progress', 'acknowledgement'}:
            return dict(reply) if reply._progress.publish(publish, reply['text']) else None
        dispatch = getattr(reply, '_mission_callback', None)
        if dispatch is not None:
            # Consume before calling transport, even if publication fails.
            reply._mission_callback = None
            try:
                reply['text'] = dispatch()
            except Exception:
                reply['text'] = (
                    '실행 요청의 처리 여부를 확인하지 못했어요. '
                    '자동으로 다시 보내지 않을게요.'
                )
                reply['kind'] = 'error'
        prefetch = getattr(reply, '_prefetch', None)
        if prefetch is not None:
            reply._prepared_hint = (
                prefetch.hint(reply['text']) if reply['kind'] == 'answer' else {}
            )
            if not reply._prepared_hint:
                prefetch.cancel()
        published = False
        try:
            if ticket is not None:
                reply['text'] = ticket['progress'].final_text(reply['text'])
            published = publish(reply['text'])
        finally:
            worker._release_prefetch(reply, published=published)
            if ticket is not None:
                ticket['state'] = 'published' if published else 'cancelled'
                worker._condition.notify_all()
        if published:
            if trace is not None:
                trace.set_flag('prefetch_hit', bool(getattr(reply, '_prepared_hint', None)))
                trace.mark_tts_published()
                if ticket is not None:
                    trace.set_flag('delivery_before_save', True)
                    waterfall = trace.snapshot_compact()
                else:
                    record = trace.finalize(worker.turn_metrics)
                    waterfall = record.to_compact_dict()
                if isinstance(reply, _DialogueReply):
                    reply._waterfall = waterfall
            activate = getattr(reply, '_on_publish', None)
            if isinstance(reply, _DialogueReply):
                reply._on_publish = None
            if activate is not None:
                activate()
            return dict(reply)
        if trace is not None and ticket is None and not trace.finalized:
            trace.finalize(worker.turn_metrics, outcome='cancelled')
        return None


def deliver_before_save(
    worker: Any,
    result: Any,
    utterance_id: str,
    conversation_id: str,
    generation: int,
    progress: Any,
    prefetch: Any,
    *,
    clock: Callable[[], float],
    trace: Any = None,
) -> bool:
    """Let the ROS thread publish within the worker's validation transaction."""
    if result.decision.ends_conversation:
        # The committed result sends a turn control instead of a speech ticket.
        return False
    deadline = clock() + 0.25
    if not worker._condition.acquire(timeout=0.25):
        return False
    try:
        if worker._closing or worker._suspended or generation != worker._generation:
            return False
        text = result.decision.message
        from malbut_agent_server.mission_audio import CATALOG
        if ((result.decision.type == 'refusal' and result.decision.reason in {
                'safety:unknown_tool', 'safety:tool_unavailable',
        }) or result.decision.reason in {'unsupported_capability', 'unavailable_capability'}):
            text = CATALOG['operation.unsupported']
        reply = make_reply(utterance_id, conversation_id, text, 'answer')
        reply['delivery_before_save'] = True
        ticket = reply._delivery_ticket = {'state': 'waiting', 'progress': progress}
        reply._generation = generation
        reply._prefetch = prefetch
        if trace is not None:
            trace.mark_emitted()
            reply._turn_trace = trace
        worker._emit(reply)
        worker._condition.wait_for(
            lambda: ticket['state'] != 'waiting' or worker._closing
            or worker._suspended or generation != worker._generation,
            timeout=max(0.0, deadline - clock()),
        )
        if ticket['state'] == 'published':
            progress.finish()
            return True
        ticket['state'] = 'cancelled'
        # The ROS thread may already have drained it. The cancelled ticket
        # also guards that copy, so only the normal post-save reply remains.
        worker._results = deque(item for item in worker._results if item is not reply)
        return False
    finally:
        worker._condition.release()


def answer_companion(
    worker: Any,
    runtime: Any,
    utterance_id: str,
    text: str,
    progress: Any,
    generation: int,
    *,
    trace: Any = None,
) -> None:
    """Handle one companion patrol-encounter turn off-thread."""
    from malbut_agent_server.companion_dialogue import CompanionOutcome

    session = progress.companion_session
    companion_error = False
    try:
        with (
            request_scope(progress=progress),
            trace.measure('provider_ms') if trace is not None else _nullcontext(),
        ):
            provider = getattr(runtime, 'companion_provider', runtime.provider)
            message = worker._companion.answer(
                provider, session, utterance_id, text, progress.opener_context,
                scene_source=worker._scene_source,
            )
    except Exception:
        companion_error = True
        message = ERROR_RESPONSE
    if trace is not None:
        trace.set_metadata(
            outcome='error' if companion_error else 'companion',
            conversation_id=session if isinstance(session, str) else None,
        )
    receipt = worker._companion.reply_receipt()
    ended = message is CompanionOutcome.END_CONVERSATION
    progress.finish(cancelled=message is None or ended)
    finalize_outcome = None
    with worker._condition:
        worker._active_progress = None
        worker._running = False
        if worker._closing:
            if trace is not None and not trace.finalized:
                finalize_outcome = 'cancelled'
        else:
            if message is None or generation != worker._generation:
                worker._outstanding -= 1
                if trace is not None and not trace.finalized:
                    finalize_outcome = 'cancelled'
            else:
                reply = make_reply(
                    utterance_id,
                    session,
                    '' if ended else message,
                    'end_conversation' if ended else 'answer',
                )
                reply._companion_session = session

                def validate() -> None:
                    if not worker._companion.reply_allowed(session, receipt):
                        raise MemoryChangedError('companion identity or memory changed')

                reply._memory_validator = validate
                reply._generation = generation
                if trace is not None:
                    trace.mark_emitted()
                    reply._turn_trace = trace
                worker._emit(reply)
    if trace is not None and finalize_outcome is not None:
        trace.finalize(worker.turn_metrics, outcome=finalize_outcome)


def classify_interruption(
    worker: Any,
    runtime: Any,
    conversation_id: Optional[str],
    utterance_id: str,
    playback_id: str,
    text: str,
) -> Dict[str, str]:
    """Classify whether an utterance during TTS playback addresses the robot."""
    try:
        snapshot = worker._companion.snapshot()
        if snapshot is None:
            snapshot = runtime.conversation_store.snapshot(
                worker._user_id, conversation_id, limit=10,
            )
        decision = runtime.speech_addressee.classify(text, snapshot)
        if decision not in ADDRESSEE_DECISIONS:
            decision = 'unknown'
    except Exception:
        decision = 'unknown'
    return addressee_reply(utterance_id, playback_id, decision)
