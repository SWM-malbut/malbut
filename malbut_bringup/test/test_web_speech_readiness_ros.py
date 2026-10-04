"""Exercise web speech readiness over DDS without starting hardware or missions."""

import time
import json

import pytest

from malbut_bringup.web_panel import PanelData, RosBridge


def test_connection_display_does_not_gate_available_manager_actions(monkeypatch):
    """Whole-stack connection status is a display, not a manager Action gate."""
    rclpy = pytest.importorskip('rclpy')
    actions = pytest.importorskip('malbut_interfaces.action')
    from rclpy.action import ActionServer
    from rclpy.executors import SingleThreadedExecutor
    from rclpy.qos import DurabilityPolicy, QoSProfile
    from std_msgs.msg import String

    monkeypatch.setenv('ROS_DOMAIN_ID', '194')
    monkeypatch.setenv('ROS_LOCALHOST_ONLY', '1')
    rclpy.init()
    manager = rclpy.create_node('speech_readiness_test_manager')
    speech = rclpy.create_node('speech_readiness_test_stt')
    server = ActionServer(manager, actions.ExecuteMission, '/malbut/mission/execute',
                          lambda _: actions.ExecuteMission.Result())
    publisher = speech.create_publisher(String, '/malbut/speech/status', QoSProfile(
        depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL))
    publisher.publish(String(data='ready'))
    # Missions open only after the manager leaves BOOTING.
    from malbut_interfaces.msg import SystemState
    state_publisher = manager.create_publisher(SystemState, '/malbut/state', QoSProfile(
        depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL))
    state_publisher.publish(SystemState(system_state=SystemState.IDLE))
    connections = manager.create_publisher(String, '/malbut/bringup/status', QoSProfile(
        depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL))
    bridge = RosBridge(PanelData(), node_name='speech_readiness_test_web')
    # Represent an owned launch without spawning any child process.
    bridge.runtime._status.update(state='RUNNING', mode='navigation')
    executor = SingleThreadedExecutor()
    for node in (bridge.node, manager, speech):
        executor.add_node(node)

    def wait_for(ready):
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            executor.spin_once(timeout_sec=0.05)
            bridge._refresh()
            if bridge.data.snapshot()['runtime']['ready'] is ready:
                return
        pytest.fail(f'Web readiness did not become {ready}')

    try:
        assert not bridge.data.snapshot()['runtime']['ready']
        connections.publish(String(data=json.dumps({'state': 'READY', 'missing': []})))
        wait_for(True)
        deadline = time.monotonic() + 5.0
        while not bridge.speech_ready and time.monotonic() < deadline:
            executor.spin_once(timeout_sec=0.05)
        assert bridge.speech_ready
        speech.destroy_publisher(publisher)
        deadline = time.monotonic() + 5.0
        while (bridge.node.count_publishers('/malbut/speech/status')
               and time.monotonic() < deadline):
            executor.spin_once(timeout_sec=0.05)
        assert bridge.node.count_publishers('/malbut/speech/status') == 0
        connections.publish(String(data=json.dumps({
            'state': 'WAITING', 'missing': ['init:malbut_stt']})))
        wait_for(False)
        assert bridge.data.snapshot()['servers']['manager']
        assert bridge.data.snapshot()['runtime']['waiting'] == ['init:malbut_stt']
    finally:
        bridge.runtime.close()
        executor.shutdown()
        server.destroy()
        for node in (bridge.node, manager, speech):
            node.destroy_node()
        rclpy.shutdown()
