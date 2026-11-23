"""Dialogue reply publishing, TTS preparation draining, and mission events."""

import json
from typing import Any, Callable, Dict, Optional
from uuid import NAMESPACE_URL, uuid5

from malbut_agent_server.communication.addressee import resolve_addressee_result


def drain_preparations(node: Any) -> None:
    """Publish optional TTS preparation and cancellation cache hints."""
    if node._preparation is None:
        return
    for event in node.dialogue.drain_preparations():
        if node._closing and event.operation != 'cancel':
            continue
        try:
            node._preparation.publish(node._preparation_type(
                operation=(
                    node._preparation_type.PREPARE
                    if event.operation == 'prepare'
                    else node._preparation_type.CANCEL
                ),
                request_id=event.request_id,
                generation_id=event.generation_id,
                prepared_id=event.prepared_id,
                text=event.text,
            ))
        except Exception:
            # This optional cache hint cannot block final speech.
            node.get_logger().warning('speech_preparation_publish_skipped')


def end_voice_turn(node: Any, reply: Dict[str, Any], *, string_msg_cls: Any) -> bool:
    """End either a companion patrol chat turn or a standard voice dialogue."""
    uid = reply['utterance_id']
    if uid != node._awaiting_reply:
        return False
    companion = getattr(reply, '_companion_session', None)
    if companion is not None:
        chat = node._patrol_chat
        if chat is None or chat.request_id != companion or chat.reply_id != uid:
            return False
        chat.end_by_user()
    else:
        node._dialogue_end.publish(string_msg_cls(data=uid))
        node.say_dialogue_end()
    return True


def _make_reply_speaker(
    node: Any,
    response: Dict[str, Any],
    *,
    prerecorded_audio: bool,
) -> Callable[[str], bool]:
    """Bind response metadata cleanly without capturing a loop variable."""
    interim = response.get('kind') in {'progress', 'acknowledgement'}
    # Prerecorded TTS coalesces waiting/start notices for the
    # same utterance and preserves a start before a fast result.
    # Keep legacy synthesized receipts on their old correlation.
    request_id = (
        'ack:' + str(uuid5(NAMESPACE_URL, response['utterance_id']))
        if response.get('kind') == 'acknowledgement' and not prerecorded_audio
        else response.get('utterance_id', '')
    )
    prepared_hint = getattr(response, '_prepared_hint', {})

    def _speak(text: str) -> bool:
        return bool(node.say(
            text,
            interim=interim,
            request_id=request_id,
            **prepared_hint,
        ))

    return _speak


def drain_dialogue(
    node: Any,
    *,
    settings: Any,
    prerecorded_audio: bool,
    response_cls: Any,
) -> None:
    """Drain dialogue worker outputs onto ROS topics and addressee futures."""
    if node._closing:
        return
    drain_preparations(node)
    node.weather_query.drain()
    if node.dialogue.startup_error:
        node.get_logger().error('speech_dialogue startup failed')
        raise RuntimeError('speech_dialogue_startup_failed')
    node._start_speech_inputs()
    for response in node.dialogue.drain(validate=False):
        if response.get('kind') == 'addressee':
            resolve_addressee_result(node, response, response_cls=response_cls)
            continue
        if (node._patrol_chat is not None
                and not node._patrol_chat.allows_reply(response.get('utterance_id'))):
            if node._preparation is not None:
                node.dialogue.discard_reply(response)
            if response.get('utterance_id') == node._awaiting_reply:
                node._awaiting_reply = None
            continue
        with node._announcer.direct_reply(
            getattr(response, '_mission_request_id', None),
        ) as acknowledge:
            published = node.dialogue.publish_reply(
                response,
                _make_reply_speaker(
                    node, response, prerecorded_audio=prerecorded_audio,
                ),
                end_dialogue=node._end_voice_turn,
            )
            if published is not None and published['kind'] != 'end_conversation':
                acknowledge(published['text'])
        if published is None:
            continue
        if (response.get('kind') not in {'progress', 'acknowledgement'}
                and response.get('utterance_id') == node._awaiting_reply):
            node._awaiting_reply = None
        if published['kind'] == 'end_conversation':
            node.get_logger().info(json.dumps({
                'event': 'voice_dialogue_ended',
                'utterance_id': published['utterance_id'],
            }))
            continue
        logged = (
            {key: published[key] for key in ('utterance_id', 'kind')}
            if getattr(response, '_companion_session', None)
            else published
        )
        waterfall = getattr(response, '_waterfall', None)
        node.get_logger().info(json.dumps({
            'event': 'dialogue_response_published',
            **logged,
            **({'prefetch': dict(response._prefetch.metrics)}
               if getattr(response, '_prefetch', None) is not None else {}),
            **({'waterfall': waterfall} if waterfall is not None else {}),
        }, ensure_ascii=False))
        if settings.turn_metrics_snapshot_path and waterfall is not None:
            try:
                node.turn_metrics.write_snapshot(
                    settings.turn_metrics_snapshot_path,
                )
            except Exception:
                node.get_logger().warning('turn_metrics_snapshot_write_failed')


def handle_mission_event(
    node: Any,
    event: Dict[str, Any],
    *,
    on_event: Optional[Callable[[Dict[str, Any]], None]],
) -> None:
    """Route Manager mission events to voice missions, weather, and announcer."""
    if node.speech_missions is not None:
        node.speech_missions.handle(event)
    node.get_logger().info(json.dumps(
        {'event': 'mission_event', **event}, ensure_ascii=False,
    ))
    weather_event = (
        node.weather_query is not None
        and node.weather_query.handle(event)
    )
    text = None if weather_event else node._announcer.handle(event)
    if text is not None:
        node.get_logger().info(json.dumps({
            'event': 'speech_published',
            'request_id': event['request_id'], 'text': text,
        }, ensure_ascii=False))
    if on_event is not None:
        on_event(dict(event))
