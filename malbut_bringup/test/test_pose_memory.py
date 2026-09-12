"""Validate persistent localization without launching or moving a robot."""

from concurrent.futures import Future
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from geometry_msgs.msg import PoseWithCovarianceStamped
from lifecycle_msgs.msg import State
from malbut_bringup.pose_memory import PoseMemory
from malbut_bringup.pose_store import map_identity, read_pose, valid_pose, write_pose
import pytest
from rclpy.time import Time


def _pose():
    return {'frame_id': 'map', 'x': 1.0, 'y': 2.0, 'yaw': 0.3,
            'covariance': [0.1 if i % 7 == 0 else 0.0 for i in range(36)]}


def test_pose_matches_map_contents_not_just_filename(tmp_path):
    """Replacing an image at the same path must invalidate the old pose."""
    image = tmp_path / 'home.pgm'
    image.write_bytes(b'P5\n1 1\n255\n\xff')
    source = tmp_path / 'home.yaml'
    source.write_text('image: home.pgm\nresolution: 0.05\n')
    identity = map_identity(source)
    path = tmp_path / 'localization/last_pose.yaml'
    write_pose(path, identity, _pose())
    saved = read_pose(path, identity)
    assert saved['x'] == 1.0
    assert saved['covariance'] == _pose()['covariance']
    assert 'saved_at' in saved
    image.write_bytes(b'P5\n1 1\n255\n\x00')
    assert read_pose(path, map_identity(source)) is None
    assert list(path.parent.iterdir()) == [path]


@pytest.mark.parametrize('update', [
    {'x': float('nan')}, {'yaw': float('inf')}, {'y': 'bad'},
    {'covariance': [0] * 35}, {'covariance': [-1] * 36}, {'frame_id': 'odom'},
])
def test_invalid_pose_never_overwrites_existing_record(tmp_path, update):
    """Bad localization data cannot destroy the previous usable record."""
    path = tmp_path / 'pose.yaml'
    write_pose(path, 'map', _pose())
    before = path.read_bytes()
    with pytest.raises(ValueError):
        write_pose(path, 'map', {**_pose(), **update})
    assert path.read_bytes() == before


def test_invalid_and_missing_files_are_ignored(tmp_path):
    """A corrupt record falls back to manual initialization."""
    path = tmp_path / 'pose.yaml'
    assert read_pose(path, 'map') is None
    for value in ('[broken', 'null', '123', '{}'):
        path.write_text(value)
        assert read_pose(path, 'map') is None


def _node(monkeypatch, tmp_path):
    node = object.__new__(PoseMemory)
    node.path = tmp_path / 'last_pose.yaml'
    node.identity = 'map'
    node.period = 5.0
    node.saved = _pose()
    node.restore_done = False
    node.latest = None
    node.received = 0.0
    node.saved_received = 0.0
    node.initial_stamp = 0
    node.active = True
    node.publisher = Mock()
    node.client = Mock()
    node.client.service_is_ready.return_value = True
    node.get_logger = Mock()
    node.get_clock = lambda: SimpleNamespace(now=lambda: Time(seconds=100))
    node.get_subscriptions_info_by_topic = lambda _: [SimpleNamespace(node_name='amcl')]
    monkeypatch.setattr('malbut_bringup.pose_memory.time.monotonic', lambda: 100.0)
    node.future = Future()
    node.future.set_result(SimpleNamespace(current_state=State(id=3)))
    return node


def test_restore_once_and_only_after_amcl_is_active(monkeypatch, tmp_path):
    """Startup memory is not repeatedly injected into localization."""
    node = _node(monkeypatch, tmp_path)
    node.future = Future()
    node.future.set_result(SimpleNamespace(current_state=State(id=2)))
    node._restore()
    node.publisher.publish.assert_not_called()
    node.future = Future()
    node.future.set_result(SimpleNamespace(current_state=State(id=3)))
    node._restore()
    restored = node.publisher.publish.call_args.args[0]
    assert restored.header.frame_id == 'map'
    assert restored.pose.pose.position.x == 1
    assert list(restored.pose.covariance) == _pose()['covariance']
    node._restore()
    node.publisher.publish.assert_called_once()


def test_manual_initialization_wins(monkeypatch, tmp_path):
    """An operator request must never be replaced by a stored pose."""
    node = _node(monkeypatch, tmp_path)
    msg = PoseWithCovarianceStamped()
    msg.header.frame_id = 'map'
    node._initialpose(msg)
    node._restore()
    node.publisher.publish.assert_not_called()


def test_only_fresh_amcl_estimates_are_saved(monkeypatch, tmp_path):
    """Avoid writing stale, inactive or non-map estimates to disk."""
    node = _node(monkeypatch, tmp_path)
    msg = PoseWithCovarianceStamped()
    msg.header.frame_id = 'map'
    msg.header.stamp.sec = 10
    msg.pose.pose.orientation.w = 1.0
    node._receive(msg)
    assert node.latest is None
    msg.header.stamp.sec = 100
    node.active = False
    node._receive(msg)
    assert node.latest is not None
    node.save_latest()
    assert not Path(node.path).exists()
    node.active = True
    node._receive(msg)
    assert valid_pose(node.latest)
    node.save_latest()
    assert Path(node.path).is_file()
    assert node.saved_received == 100
    before = Path(node.path).stat().st_mtime_ns
    node.save_latest()
    assert Path(node.path).stat().st_mtime_ns == before


def test_our_own_initialpose_subscription_does_not_count(monkeypatch, tmp_path):
    """Wait for AMCL, not just the pose-memory subscriber itself."""
    node = _node(monkeypatch, tmp_path)
    node.get_subscriptions_info_by_topic = lambda _: [SimpleNamespace(node_name='pose_memory')]
    node._restore()
    node.publisher.publish.assert_not_called()


def test_estimate_arriving_before_get_state_is_not_overridden(monkeypatch, tmp_path):
    """Preserve a fresh stationary AMCL pose received during startup discovery."""
    node = _node(monkeypatch, tmp_path)
    node.active = False
    msg = PoseWithCovarianceStamped()
    msg.header.frame_id = 'map'
    msg.header.stamp.sec = 100
    msg.pose.pose.orientation.w = 1.0
    node._receive(msg)
    node._restore()
    node.publisher.publish.assert_not_called()
    assert node.restore_done


def test_manual_pose_rejects_queued_older_estimate(monkeypatch, tmp_path):
    """Do not save an estimate computed before the user's pose correction."""
    node = _node(monkeypatch, tmp_path)
    msg = PoseWithCovarianceStamped()
    msg.header.frame_id = 'map'
    node._initialpose(msg)
    msg.header.stamp.sec = 99
    msg.pose.pose.orientation.w = 1.0
    node._receive(msg)
    assert node.latest is None


def test_pending_state_request_does_not_block_restarted_amcl(monkeypatch, tmp_path):
    """Forget an unanswered request when AMCL disappears, then query its replacement."""
    node = _node(monkeypatch, tmp_path)
    abandoned = Future()
    node.future = abandoned
    node.client.service_is_ready.return_value = False
    node._restore()
    node.client.remove_pending_request.assert_called_once_with(abandoned)
    assert abandoned.cancelled()
    assert node.future is None
    assert not node.active
    node.publisher.publish.assert_not_called()

    node._restore()  # No duplicate cleanup while the server remains absent.
    node.client.remove_pending_request.assert_called_once()
    replacement = Future()
    node.client.service_is_ready.return_value = True
    node.client.call_async.return_value = replacement
    node._restore()
    node.client.call_async.assert_called_once()
    assert node.future is replacement
    replacement.set_result(SimpleNamespace(current_state=State(id=3)))
    node._restore()
    assert node.active
    node.publisher.publish.assert_called_once()
