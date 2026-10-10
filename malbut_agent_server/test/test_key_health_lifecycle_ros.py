"""A retired node must not receive another client's key-health update."""

import pytest

rclpy = pytest.importorskip('rclpy')
from rclpy.node import Node

from malbut_agent_server.ros_key_health import KeyHealthPublisher
from malbut_agent_server.service_keys import ManagedKey


def test_closed_publisher_ignores_late_health_callback(monkeypatch, tmp_path):
    monkeypatch.setenv('ROS_DOMAIN_ID', '189')
    monkeypatch.setenv('ROS_LOCALHOST_ONLY', '1')
    rclpy.init()
    node = Node('key_health_lifecycle_test')
    key = ManagedKey('kma', environ={}, directory=tmp_path)
    observer = KeyHealthPublisher(node, key)
    late_callback = observer._publish
    try:
        key.report('missing')
        observer.close()
        observer.close()
        node.destroy_node()
        node = None
        # Both a new report and an already-copied callback can arrive after close.
        key.report('ok')
        late_callback('kma', 'invalid', 'authentication_failed')
        assert not key._listeners
    finally:
        observer.close()
        if node is not None:
            node.destroy_node()
        rclpy.shutdown()
