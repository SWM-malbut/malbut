"""Fall-check window tracking and pre-question spoken overrides."""

import json
from typing import Any

from malbut_agent_server.fall_check_watch import (
    FALL_CHECK_BUSY,
    FALL_CHECK_HELP,
    FALL_CHECK_START,
    FALL_CHECK_STOPPED,
    asks_for_help,
    asks_to_stop,
)


def watch_fall_check(
    node: Any,
    *,
    qos_profile_cls: Any,
    reliability_policy: Any,
    durability_policy: Any,
) -> Any:
    """Follow the fall check like the app's move buttons (2026-10-12)."""
    from malbut_agent_server.fall_check_watch import (
        FALL_CHECK_STATUS_TOPIC,
        VOICE_TOPIC,
        FallCheckWatch,
    )
    from malbut_interfaces.msg import SystemState  # type: ignore[attr-defined]
    from malbut_interfaces.srv import ControlSpeechPlayback  # type: ignore[attr-defined]
    from std_msgs.msg import String

    watch = FallCheckWatch()
    node.create_subscription(
        String, FALL_CHECK_STATUS_TOPIC,
        lambda message: watch.observe_status(message.data), 10,
    )
    node._fall_voice = node.create_publisher(String, VOICE_TOPIC, 10)
    node._fall_voice_message = String
    node._speech_stop = node.create_client(
        ControlSpeechPlayback, '/malbut/speech/playback_control',
    )
    latched = qos_profile_cls(depth=1)
    latched.reliability = reliability_policy.RELIABLE
    latched.durability = durability_policy.TRANSIENT_LOCAL
    node.create_subscription(
        SystemState, '/malbut/state',
        lambda message: watch.observe_missions(
            mission.capability_id
            for group in (message.active_foreground_missions,
                          message.suspended_missions, message.pending_missions)
            for mission in group),
        latched,
    )
    return watch


def follow_fall_window(node: Any) -> None:
    """Stop the conversation, saying so, when a fall check begins (2026-10-13)."""
    if node._closing or node._fall_check is None or node.dialogue is None:
        return
    inside = node._fall_check.in_window()
    if inside == node._in_fall_window:
        return
    node._in_fall_window = inside
    if not inside:
        if node.situation is None or not node.situation.active:
            node.dialogue.resume()
        return
    if node._patrol_chat is not None:
        node._patrol_chat.end()
    node.dialogue.suspend()
    # Played under the interrupted utterance's ID, it also ends STT's wait
    # for that reply; otherwise the microphone would stay closed.
    waiting, node._awaiting_reply = node._awaiting_reply, None
    if node.situation is not None and node.situation.active:
        # Confirmation already owns the speaker/microphone: STOP_ALL or the
        # pre-check notice would cut off its own question playback.
        return

    def announce(_done: Any = None) -> None:
        if (node._closing or not node._in_fall_window
                or (node.situation is not None and node.situation.active)):
            return
        node.say(FALL_CHECK_START, request_id=waiting or '')
        node.get_logger().info(json.dumps({
            'event': 'fall_check_announced', 'interrupted': waiting,
        }, ensure_ascii=False))

    if node._speech_stop.service_is_ready():
        # The notice goes after the stop, or the stop would cut it too.
        from malbut_interfaces.srv import ControlSpeechPlayback  # type: ignore[attr-defined]
        node._speech_stop.call_async(ControlSpeechPlayback.Request(
            playback_id='', command=ControlSpeechPlayback.Request.STOP_ALL,
        )).add_done_callback(announce)
    else:
        announce()


def answer_during_fall_check(node: Any, utterance_id: str, text: str) -> None:
    """Before the question: help alerts at once, a stop ends the check, else wait."""
    kind = (
        'help' if asks_for_help(text)
        else 'stop' if asks_to_stop(text) else None
    )
    if kind is None:
        node.say(FALL_CHECK_BUSY, request_id=utterance_id)
        return
    node._fall_voice.publish(node._fall_voice_message(data=json.dumps(
        {'kind': kind, 'utterance_id': utterance_id},
    )))
    node.say(
        FALL_CHECK_HELP if kind == 'help' else FALL_CHECK_STOPPED,
        request_id=utterance_id,
    )
    node.get_logger().info(json.dumps({
        'event': 'fall_check_voice', 'kind': kind, 'utterance_id': utterance_id,
    }, ensure_ascii=False))
