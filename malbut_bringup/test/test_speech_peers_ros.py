"""Verify speech discovery against generated interfaces and a real ROS graph."""

import pytest

from malbut_bringup.speech_preflight import check_interfaces, wait_for_control, wait_for_peers


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


@pytest.mark.parametrize('mode', ['manager', 'autoslam'])
def test_control_gate_waits_for_real_action_server_and_manager_boot(mode, monkeypatch):
    """Discover control readiness without submitting even a test motion Goal."""
    from malbut_interfaces.action import AutoSlam, ExecuteMission
    from rclpy.action import ActionClient, ActionServer
    from rclpy.context import Context
    from rclpy.node import Node
    from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy

    def forbidden(*args, **kwargs):
        pytest.fail('readiness must not send a mission Goal')

    monkeypatch.setattr(ActionClient, 'send_goal_async', forbidden)
    context = Context()
    rclpy.init(context=context)
    node = Node('control_gate_test', context=context)
    server = None
    try:
        with pytest.raises(RuntimeError, match='robot_control_not_ready'):
            wait_for_control(mode, 0.2)
        assert not rclpy.ok()
        action_type, name = {
            'manager': (ExecuteMission, '/malbut/mission/execute'),
            'autoslam': (AutoSlam, '/autoslam'),
        }[mode]
        server = ActionServer(node, action_type, name, execute_callback=forbidden)
        if mode == 'manager':
            state = node.create_publisher(messages.SystemState, '/malbut/state', QoSProfile(
                depth=1, reliability=ReliabilityPolicy.RELIABLE,
                durability=DurabilityPolicy.TRANSIENT_LOCAL))
            state.publish(messages.SystemState(system_state=messages.SystemState.BOOTING))
            with pytest.raises(RuntimeError, match='robot_control_not_ready'):
                wait_for_control(mode, 0.5)
            state.publish(messages.SystemState(system_state=messages.SystemState.IDLE))
        wait_for_control(mode, 5.0)
        assert not rclpy.ok()
    finally:
        if server is not None:
            server.destroy()
        node.destroy_node()
        rclpy.shutdown(context=context)
