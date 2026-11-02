"""Exercise web speech readiness over DDS without starting hardware or missions."""

import json
from pathlib import Path
import time
from unittest.mock import Mock

import pytest

from malbut_bringup.web_panel import PanelData, RosBridge


def test_cloud_entrypoint_does_not_subscribe_to_lan_video(monkeypatch, tmp_path):
    """Cloud needs no pixels; the LAN panel still receives both live streams."""
    rclpy = pytest.importorskip('rclpy')
    pytest.importorskip('malbut_interfaces.action')
    from rclpy.executors import SingleThreadedExecutor
    from rclpy.qos import qos_profile_sensor_data
    from sensor_msgs.msg import CompressedImage, Image
    from malbut_bringup import cloud_sync

    monkeypatch.setenv('ROS_DOMAIN_ID', '194')
    monkeypatch.setenv('ROS_LOCALHOST_ONLY', '1')
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(cloud_sync, 'read_device_token', lambda _: (
        'hc1.11111111-1111-4111-8111-111111111111.' + 'a' * 64))
    client, sync = Mock(), Mock()
    monkeypatch.setattr(cloud_sync, 'CloudClient', client)
    monkeypatch.setattr(cloud_sync, 'CloudSync', sync)
    bridges = []

    def record_bridge(*args, **kwargs):
        bridge = RosBridge(*args, **kwargs)
        bridges.append(bridge)
        return bridge

    monkeypatch.setattr(cloud_sync, 'RosBridge', record_bridge)

    def check_streams(executor):
        cloud = bridges[0]
        local = RosBridge(PanelData(), node_name='lan_video_test')
        camera = rclpy.create_node('video_test_camera')
        executor.add_node(local.node)
        executor.add_node(camera)
        raw = camera.create_publisher(Image, cloud.topics['rgb_topic'], qos_profile_sensor_data)
        debug = camera.create_publisher(
            CompressedImage, cloud.topics['debug_topic'], qos_profile_sensor_data)
        try:
            for bridge, expected in ((cloud, set()), (local, {
                    cloud.topics['rgb_topic'], cloud.topics['debug_topic']})):
                image_topics = {
                    subscription.topic_name for subscription in bridge.node.subscriptions
                    if subscription.msg_type in (Image, CompressedImage)}
                assert image_topics == expected
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                raw.publish(Image(width=1, height=1, encoding='rgb8', step=3,
                                  data=b'\x10\x20\x30'))
                debug.publish(CompressedImage(format='jpeg', data=b'\xff\xd8\xff\xd9'))
                executor.spin_once(timeout_sec=0.05)
                if set(local.data.frames) == {'raw', 'debug'}:
                    break
            assert set(local.data.frames) == {'raw', 'debug'}
            assert local.data.jpeg('raw').startswith(b'\xff\xd8')
            assert local.data.jpeg('debug') == b'\xff\xd8\xff\xd9'
            assert cloud.data.frames == {}
            assert cloud.data.snapshot()['video_age_s'] == {}
        finally:
            for node in (local.node, camera):
                executor.remove_node(node)
                node.destroy_node()
            local.runtime.close()

    monkeypatch.setattr(SingleThreadedExecutor, 'spin', check_streams)
    cloud_sync.main()
    assert sync.call_args.kwargs['homecam'] is not None


@pytest.mark.parametrize('subscribe_images', [True, False])
def test_speech_presence_does_not_gate_manager_readiness(monkeypatch, subscribe_images):
    """Speech status is observable, but STT exit does not block other features."""
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
    connections = manager.create_publisher(String, '/malbut/bringup/status', QoSProfile(
        depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL))
    connections.publish(String(data=json.dumps({'state': 'READY', 'missing': []})))
    # Missions open only after the manager leaves BOOTING.
    from malbut_interfaces.msg import SystemState
    state_publisher = manager.create_publisher(SystemState, '/malbut/state', QoSProfile(
        depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL))
    state_publisher.publish(SystemState(system_state=SystemState.IDLE))
    bridge = RosBridge(PanelData(), node_name='speech_readiness_test_web',
                       subscribe_images=subscribe_images)
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
        bridge._refresh()
        assert bridge.data.snapshot()['runtime']['ready']
    finally:
        bridge.runtime.close()
        executor.shutdown()
        server.destroy()
        for node in (bridge.node, manager, speech):
            node.destroy_node()
        rclpy.shutdown()
