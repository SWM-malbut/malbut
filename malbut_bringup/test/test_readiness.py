"""Exercise startup admission checks with no DDS, GPU, or robot processes."""

from concurrent.futures import Future
from types import SimpleNamespace
from unittest.mock import Mock

from lifecycle_msgs.msg import State
from rclpy.clock import ClockType
from rclpy.time import Time
from sensor_msgs.msg import Image
from vision_msgs.msg import Detection3DArray

from malbut_bringup.readiness import RobotReadiness


def _node(monkeypatch):
    node = object.__new__(RobotReadiness)
    node.settings = {'robot_frame': 'base_footprint', 'global_frame': 'map',
                     'navigation': False}
    node.timeout = 3.0
    node.ready = False
    node.seen = {'scan': 10.0, 'odom': 10.0, 'camera_info': 10.0}
    node.frames = {'scan': 'lidar', 'odom': 'odom', 'camera_info': 'camera_optical'}
    node.fixed = set()
    node.action_clients = []
    node.lifecycle = {}
    node.tf = Mock()
    node.tf.can_transform.return_value = True
    node.last_missing = None
    node.get_logger = Mock()
    node.get_clock = lambda: SimpleNamespace(
        now=lambda: Time(seconds=10, clock_type=ClockType.ROS_TIME))
    monkeypatch.setattr('malbut_bringup.readiness.time.monotonic', lambda: 10.0)
    return node


def test_ready_requires_data_and_transforms(monkeypatch):
    """A server/Topic name alone is not a readiness signal."""
    node = _node(monkeypatch)
    node.seen['rgb'] = None
    node.check()
    assert not node.ready
    node.seen['rgb'] = 10.0
    node.tf.can_transform.return_value = False
    node.check()
    assert not node.ready
    node.tf.can_transform.return_value = True
    node.check()
    assert node.ready


def test_old_sensor_message_does_not_count_as_fresh_data(monkeypatch):
    """Delivery time does not hide an old camera header."""
    node = _node(monkeypatch)
    node.seen['rgb'] = None
    message = Image(width=1, height=1, data=[0, 0, 0])
    message.header.frame_id = 'camera_optical'
    message.header.stamp.sec = 1
    node._receive('rgb', message)
    assert node.seen['rgb'] is None
    message.header.stamp.sec = 10
    node._receive('rgb', message)
    assert node.seen['rgb'] == 10.0


def test_empty_detection_array_is_valid_no_person_required(monkeypatch):
    """Booting must not require somebody to stand in front of the camera."""
    node = _node(monkeypatch)
    node.seen['perception'] = None
    message = Detection3DArray()
    message.header.frame_id = 'camera_optical'
    message.header.stamp.sec = 10
    node._receive('perception', message)
    assert node.seen['perception'] == 10.0


def test_stale_stream_stays_not_ready(monkeypatch):
    """A camera that stopped during boot must not admit new missions."""
    node = _node(monkeypatch)
    node.seen['rgb'] = 6.0
    node.check()
    assert not node.ready


def test_navigation_needs_matching_map_and_active_nav2(monkeypatch):
    """An inactive Nav2 Action server must not open manager admission."""
    node = _node(monkeypatch)
    node.settings['navigation'] = True
    node.fixed.add('map')
    node.seen['map'] = 0.0  # Static map age is not a sensor-stream timeout.
    node.frames['map'] = 'wrong_map'
    node.check()
    assert not node.ready
    node.frames['map'] = 'map'
    future = Future()
    future.set_result(SimpleNamespace(current_state=State(id=State.PRIMARY_STATE_INACTIVE)))
    service = Mock()
    service.service_is_ready.return_value = True
    node.lifecycle['controller_server'] = [service, future, False]
    node.check()
    assert not node.ready
    future = Future()
    future.set_result(SimpleNamespace(current_state=State(id=State.PRIMARY_STATE_ACTIVE)))
    node.lifecycle['controller_server'][1] = future
    node.check()
    assert node.ready
