"""One synthetic ROS image through the production subscriber, with a fake model."""
from types import SimpleNamespace
from time import monotonic

import pytest

rclpy = pytest.importorskip('rclpy')
from rclpy.node import Node
from rclpy.parameter import Parameter
from rclpy.executors import SingleThreadedExecutor
from rclpy.qos import qos_profile_sensor_data
from sensor_msgs.msg import Image

from malbut_agent_server import camera_view
from malbut_agent_server.config import Settings


def test_camera_subscription_uses_fresh_frame_and_honors_disable(monkeypatch):
    calls = []
    monkeypatch.setattr(camera_view, 'OpenAICameraAnswerer',
        lambda key, model, url: SimpleNamespace(
            answer=lambda question, jpeg: calls.append((question, jpeg)) or '빨간 화면이에요.'))
    rclpy.init()
    node = Node('reachy_camera_test', parameter_overrides=[
        Parameter('camera_view_enabled', value=True),
        Parameter('camera_view_topic', value='/reachy_camera_test/image')])
    executor = SingleThreadedExecutor()
    executor.add_node(node)
    try:
        view = camera_view.configure_ros_camera(node, Settings(provider='openai'))
        publisher = node.create_publisher(Image, '/reachy_camera_test/image', qos_profile_sensor_data)
        image = Image(height=32, width=32, encoding='rgb8', step=96,
                      data=list(bytes([255, 0, 0])*1024))
        deadline = monotonic()+3
        while view._frame is None:
            assert monotonic() < deadline
            image.header.stamp = node.get_clock().now().to_msg()
            publisher.publish(image)
            executor.spin_once(timeout_sec=.01)
        assert view.answer('지금 뭐가 보여?') == '빨간 화면이에요.'
        assert len(calls) == 1 and calls[0][1].startswith(b'\xff\xd8')
        node.set_parameters([Parameter('camera_view_enabled', value=False)])
        with pytest.raises(ValueError, match='disabled'):
            view.answer('다시 봐줘')
        assert len(calls) == 1
    finally:
        executor.remove_node(node)
        node.destroy_node()
        executor.shutdown()
        rclpy.shutdown()
