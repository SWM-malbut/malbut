"""Check map-bound pose saving without starting ROS nodes or robot processes."""

from datetime import datetime
import hashlib
import math
from threading import RLock
from types import SimpleNamespace
from unittest.mock import Mock

from geometry_msgs.msg import TransformStamped
import pytest
from rclpy.time import Time
import yaml

from malbut_autoslam.autoslam_node import AutoSlamNode, map_base
from malbut_autoslam.saved_pose import write_mapping_pose


@pytest.fixture
def saved_map(tmp_path):
    """Create the two files returned by a successful map saver."""
    path = tmp_path / 'home.yaml'
    path.write_text('image: home.pgm\nresolution: 0.05\n')
    path.with_suffix('.pgm').write_bytes(b'P5\n1 1\n255\n\xfe')
    return path


def test_saved_pose_matches_map_identity_and_initial_covariance(saved_map):
    """Persist the agreed handoff schema, not a synthetic high-confidence pose."""
    destination = write_mapping_pose(saved_map, 1.5, 2.0, -0.7)
    assert destination == saved_map.with_suffix('.pose.yaml')
    data = yaml.safe_load(destination.read_text())
    expected = hashlib.sha256(
        saved_map.read_bytes() + saved_map.with_suffix('.pgm').read_bytes()).hexdigest()
    assert data['map_id'] == expected
    assert data['frame_id'] == 'map'
    assert (data['x'], data['y'], data['yaw']) == (1.5, 2.0, -0.7)
    assert len(data['covariance']) == 36
    assert data['covariance'][0] == data['covariance'][7] == 0.25
    assert data['covariance'][35] == pytest.approx((math.pi / 12) ** 2)
    assert datetime.fromisoformat(data['saved_at']).tzinfo is not None


def test_pose_write_failure_preserves_record_and_map(saved_map, monkeypatch):
    """An atomic replacement failure cannot truncate the map or prior record."""
    destination = write_mapping_pose(saved_map, 1.0, 2.0, 0.0)
    original = destination.read_bytes()
    monkeypatch.setattr('malbut_autoslam.saved_pose.os.replace', Mock(
        side_effect=OSError('disk error')))
    with pytest.raises(OSError, match='disk error'):
        write_mapping_pose(saved_map, 3.0, 4.0, 0.0)
    assert destination.read_bytes() == original
    assert saved_map.is_file() and saved_map.with_suffix('.pgm').is_file()
    assert not list(saved_map.parent.glob('.mapping-pose-*'))


def test_orphan_pose_cannot_be_reused_by_another_mapping_run(tmp_path):
    """Reject an old sidecar even when its map files were manually removed."""
    (tmp_path / 'home.pose.yaml').write_text('old pose')
    with pytest.raises(ValueError, match='already exists'):
        map_base(tmp_path, 'home')


def _pose_node(age=0.0, rotation=None, position=(1.5, 2.0)):
    transform = TransformStamped()
    transform.header.stamp = Time(seconds=10.0 - age).to_msg()
    transform.transform.translation.x, transform.transform.translation.y = position
    transform.transform.rotation.z, transform.transform.rotation.w = (
        rotation if rotation is not None else (math.sin(0.4), math.cos(0.4)))
    node = SimpleNamespace(
        settings={'base_frame': 'base_footprint', 'tf_timeout_s': 3.0},
        tf=Mock(), get_clock=lambda: SimpleNamespace(
            now=lambda: Time.from_msg(Time(seconds=10.0).to_msg())))
    node.tf.lookup_transform.return_value = transform
    return node


def test_save_pose_uses_fresh_map_to_base_transform(saved_map):
    """Capture yaw as well as translation in the frame expected by AMCL."""
    node = _pose_node()
    AutoSlamNode._save_pose(node, saved_map)
    data = yaml.safe_load(saved_map.with_suffix('.pose.yaml').read_text())
    assert (data['x'], data['y']) == (1.5, 2.0)
    assert data['yaw'] == pytest.approx(0.8)
    assert node.tf.lookup_transform.call_args.args[:2] == ('map', 'base_footprint')


@pytest.mark.parametrize('settings', [
    {'age': 3.1}, {'age': -3.1}, {'rotation': (0.0, 0.0)},
    {'rotation': (float('nan'), 1.0)}, {'position': (float('inf'), 2.0)},
])
def test_invalid_or_stale_transform_does_not_save_pose(saved_map, settings):
    """Do not offer unusable TF data as an initial localization estimate."""
    with pytest.raises((ValueError, RuntimeError)):
        AutoSlamNode._save_pose(_pose_node(**settings), saved_map)
    assert not saved_map.with_suffix('.pose.yaml').exists()


@pytest.mark.parametrize('pose_error', [None, OSError('disk full')])
def test_action_saves_pose_before_teardown_and_keeps_map_on_warning(
        saved_map, monkeypatch, pose_error):
    """Pose handoff failure is explicit without turning a saved map into failure."""
    module = 'malbut_autoslam.autoslam_node.'
    monkeypatch.setattr(module + 'map_base', lambda *_args: saved_map.with_suffix(''))
    monkeypatch.setattr(module + 'map_grid_from_message', lambda _message: object())
    monkeypatch.setattr(module + 'map_statistics', lambda _grid: {
        'known_area_m2': 6.0, 'free_area_m2': 6.0})
    monkeypatch.setattr(module + 'find_frontiers', lambda *_args, **_kwargs: [])
    node = Mock()
    node.lock = RLock()
    node.settings = {
        'map_directory': str(saved_map.parent), 'ready_timeout_s': 3.0,
        'exploration_period_s': 1.0, 'completion_delay_s': 0.0,
        'minimum_frontier_cells': 8, 'robot_clearance_m': 0.3,
        'minimum_goal_distance_m': 0.45,
    }
    node._snapshot.return_value = (object(), (1.5, 2.0))
    node._save.return_value = str(saved_map)

    def save_pose(map_yaml):
        node._close_runtime.assert_not_called()
        if pose_error is not None:
            raise pose_error
        write_mapping_pose(map_yaml, 1.5, 2.0, 0.8)

    node._save_pose.side_effect = save_pose
    node._explore.side_effect = lambda handle, result: AutoSlamNode._explore(node, handle, result)
    handle = Mock(is_cancel_requested=False)
    handle.request.map_name = 'home'
    result = AutoSlamNode._execute(node, handle)
    assert result.success
    assert result.map_yaml == str(saved_map)
    handle.succeed.assert_called_once_with()
    node._close_runtime.assert_called_once_with(handle)
    node._save_pose.assert_called_once_with(str(saved_map))
    if pose_error is None:
        assert saved_map.with_suffix('.pose.yaml').is_file()
        assert 'initial robot pose saved' in result.message
    else:
        assert 'WARNING' in result.message and '2D Pose Estimate' in result.message
        assert not saved_map.with_suffix('.pose.yaml').exists()
        node.get_logger().warning.assert_called_once_with(result.message)
