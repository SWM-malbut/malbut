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
    node.lifecycle_requested = {}
    node.tf = Mock()
    node.tf.can_transform.return_value = True
    node.last_missing = None
    node.status_publisher = Mock()
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
    import json
    assert 'data:rgb' in json.loads(
        node.status_publisher.publish.call_args.args[0].data)['missing']
    node.seen['rgb'] = 10.0
    node.frames['rgb'] = 'camera_optical'
    node.tf.can_transform.return_value = False
    node.check()
    assert not node.ready
    node.tf.can_transform.return_value = True
    node.check()
    assert node.ready
    assert json.loads(node.status_publisher.publish.call_args.args[0].data) == {
        'state': 'READY', 'missing': []}


def test_disconnected_depth_frame_is_not_ready(monkeypatch):
    """Receiving images alone cannot prove they can be used by the localizer."""
    node = _node(monkeypatch)
    node.seen['depth'] = 10.0
    node.frames['depth'] = 'depth_optical'
    node.tf.can_transform.side_effect = lambda base, frame, stamp: frame != 'depth_optical'
    node.check()
    assert not node.ready
    node.tf.can_transform.side_effect = None
    node.check()
    assert node.ready


def test_restarted_lifecycle_server_can_be_queried_again(monkeypatch):
    """Do not wait forever on an unanswered request to an exited server."""
    node = _node(monkeypatch)
    pending = Future()
    service = Mock()
    service.service_is_ready.return_value = False
    node.lifecycle['controller_server'] = [service, pending, False]
    node.check()
    assert pending.cancelled()
    assert node.lifecycle['controller_server'][1] is None
    assert not node.ready
    service.service_is_ready.return_value = True
    node.check()
    service.call_async.assert_called_once()


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


def test_lost_lifecycle_response_retries_without_restarting_server(monkeypatch):
    """Reproduce get_state response loss while Nav2 stays discoverable."""
    node = _node(monkeypatch)
    pending, retry = Future(), Future()
    service = Mock()
    service.service_is_ready.return_value = True
    service.call_async.side_effect = [pending, retry, Future()]
    node.lifecycle['amcl'] = [service, None, False]
    node.check()
    assert not node.ready
    node.check()
    assert service.call_async.call_count == 1

    monkeypatch.setattr('malbut_bringup.readiness.time.monotonic', lambda: 13.0)
    node.seen = {name: 13.0 for name in node.seen}
    node.check()
    assert pending.cancelled()
    service.remove_pending_request.assert_called_once_with(pending)
    assert service.call_async.call_count == 2
    assert not node.ready  # Retrying alone is not proof that Nav2 is active.
    retry.set_result(SimpleNamespace(current_state=State(id=State.PRIMARY_STATE_ACTIVE)))
    node.check()
    assert node.ready


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
