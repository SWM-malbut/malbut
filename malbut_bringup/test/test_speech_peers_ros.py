"""Verify speech discovery against generated interfaces and a real ROS graph."""

import pytest

from malbut_bringup.speech_preflight import check_interfaces, wait_for_peers


rclpy = pytest.importorskip('rclpy')
messages = pytest.importorskip('malbut_interfaces.msg')
services = pytest.importorskip('malbut_interfaces.srv')


def test_real_speech_endpoints_are_discovered_and_released():
    """Find both typed peers, then reject the graph once those peers are gone."""
    from rclpy.context import Context
    from rclpy.node import Node

    check_interfaces()
    context = Context()
    rclpy.init(context=context)
    node = Node('speech_test_peers', context=context)
    try:
        for message_type, topic in (
            (messages.SpeechTranscript, '/malbut/speech/transcript'),
            (messages.SpeechRequest, '/malbut/speech/response'),
        ):
            node.create_subscription(message_type, topic, lambda _: None, 10)
        for message_type, topic in (
            (messages.SpeechRequest, '/malbut/speech/response'),
            (messages.SpeechPlaybackStatus, '/malbut/speech/playback_status'),
        ):
            node.create_publisher(message_type, topic, 10)
        for service_type, name in (
            (services.ClassifySpeechAddressee, '/malbut/speech/classify_addressee'),
            (services.ControlSpeechPlayback, '/malbut/speech/playback_control'),
        ):
            node.create_service(service_type, name, lambda _, response: response)
        wait_for_peers(5.0)
        assert not rclpy.ok()
    finally:
        node.destroy_node()
        rclpy.shutdown(context=context)
    with pytest.raises(RuntimeError, match='speech_peers_not_ready'):
        wait_for_peers(0.5)
    assert not rclpy.ok()
