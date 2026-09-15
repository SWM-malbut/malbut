"""Guard non-blocking TF and sensor-time freshness without robot commands."""

import time
from types import MethodType, SimpleNamespace
from unittest.mock import Mock

from geometry_msgs.msg import TransformStamped
from malbut_interfaces.action import FollowPerson
from malbut_interfaces.msg import LidarClusterArray
import pytest
import rclpy
from rclpy.context import Context
from tf2_ros import Buffer, TransformException
from vision_msgs.msg import Detection3D, Detection3DArray, ObjectHypothesisWithPose

from malbut_tracking.geometry import Point2D
from malbut_tracking.person_follower_node import PersonFollowerNode


def _follower():
    parameters = {
        'minimum_confidence': 0.2,
        'prediction_horizon_s': 0.4,
        'observation_loss_debounce_s': 0.75,
        'sensor_transform_queue_timeout_s': 0.30,
    }
    follower = SimpleNamespace(
        _global_frame='map', _odometry_frame='odom', _robot_frame='base',
        _target_mode=FollowPerson.Goal.VISIBLE_PERSON,
        _target_person_id='', _detector_track_id=None,
        _active_goal=object(), _last_camera_source_stamp_ns=None,
        _last_camera_frame_s=3.0, _detection_transform_pending=False,
        _now_seconds=Mock(return_value=20.0),
        _camera_estimator=SimpleNamespace(predict=Mock(return_value=None)),
        _warn_periodically=Mock(), _accept_map_target=Mock(),
        _apply_tracking_motion=Mock(),
        get_parameter=lambda name: SimpleNamespace(value=parameters[name]),
    )
    for name in ('_target_in_global_frame', '_observation_is_current'):
        setattr(follower, name, MethodType(getattr(PersonFollowerNode, name), follower))
    return follower


def _detections(count=1, stamp_s=20):
    message = Detection3DArray()
    message.header.frame_id = 'camera'
    message.header.stamp.sec = stamp_s
    for index in range(count):
        detection = Detection3D()
        detection.id = str(index)
        detection.bbox.center.position.x = 1.0 + index / 10
        hypothesis = ObjectHypothesisWithPose()
        hypothesis.hypothesis.class_id = 'person'
        hypothesis.hypothesis.score = 0.9
        detection.results.append(hypothesis)
        message.detections.append(detection)
    return message


def _set_static_transform(buffer, parent, child, x=0.0):
    transform = TransformStamped()
    transform.header.frame_id = parent
    transform.child_frame_id = child
    transform.transform.rotation.w = 1.0
    transform.transform.translation.x = x
    buffer.set_transform_static(transform, 'latency_test')


def test_same_frame_detections_share_one_actual_tf_lookup():
    """A crowded image does not repeat identical transform lookups per person."""
    follower = _follower()
    buffer = Buffer()
    _set_static_transform(buffer, 'map', 'odom', x=2.0)
    _set_static_transform(buffer, 'odom', 'camera', x=3.0)
    follower._tf_buffer = Mock(wraps=buffer)
    observation = PersonFollowerNode._select_target_observation(
        follower, _detections(count=32),
    )
    assert observation is not None
    assert observation[1].pose.position.x == pytest.approx(6.0)
    follower._tf_buffer.lookup_transform_full.assert_called_once()
    assert follower._tf_buffer.lookup_transform_full.call_args.kwargs[
        'timeout'].nanoseconds == 0


def test_missing_shared_tf_returns_immediately_and_is_looked_up_only_once():
    """A missing transform never turns 32 detections into 32 blocking waits."""
    follower = _follower()
    follower._tf_buffer = Mock(wraps=Buffer())
    started = time.monotonic()
    observation = PersonFollowerNode._select_target_observation(
        follower, _detections(count=32),
    )
    elapsed = time.monotonic() - started
    assert observation is None
    assert follower._detection_transform_pending
    follower._tf_buffer.lookup_transform_full.assert_called_once()
    assert follower._tf_buffer.lookup_transform_full.call_args.kwargs[
        'timeout'].nanoseconds == 0
    # Wide enough for CI scheduling; the former 32 * 0.1 s loop exceeds this.
    assert elapsed < 0.5
    follower._apply_tracking_motion.assert_not_called()


def test_tf_cache_keeps_different_source_timestamps_separate():
    """Transform reuse never substitutes another observation's source time."""
    follower = _follower()
    buffer = Buffer()
    _set_static_transform(buffer, 'map', 'odom')
    _set_static_transform(buffer, 'odom', 'camera')
    follower._tf_buffer = Mock(wraps=buffer)
    message = _detections(count=2)
    message.detections[1].header.stamp.sec = 19
    PersonFollowerNode._select_target_observation(follower, message)
    assert follower._tf_buffer.lookup_transform_full.call_count == 2
    source_times = {
        call.args[3].nanoseconds
        for call in follower._tf_buffer.lookup_transform_full.call_args_list
    }
    assert source_times == {19_000_000_000, 20_000_000_000}


def test_missing_robot_tf_does_not_wait_for_its_own_executor():
    """Robot pose lookup leaves the callback free to receive later TF data."""
    follower = _follower()
    follower._tf_buffer = Mock(wraps=Buffer())
    with pytest.raises(TransformException):
        PersonFollowerNode._robot_pose(follower)
    assert follower._tf_buffer.lookup_transform.call_args.kwargs[
        'timeout'].nanoseconds == 0


@pytest.mark.parametrize('stamp_s', [10, 21])
def test_old_or_future_camera_frame_is_rejected_before_selection(stamp_s):
    """Receipt of delayed camera data must not create a fresh motion target."""
    follower = _follower()
    follower._select_target_observation = Mock()
    PersonFollowerNode._on_detections(follower, _detections(stamp_s=stamp_s))
    follower._select_target_observation.assert_not_called()
    follower._accept_map_target.assert_not_called()
    follower._apply_tracking_motion.assert_not_called()
    assert follower._last_camera_frame_s == 3.0


def test_old_lidar_frame_is_rejected_before_tracker_and_motion_updates():
    """Reject LiDAR backlog without refreshing the target or observation time."""
    follower = _follower()
    follower._lidar_clusters_received = False
    follower._obstacle_tracker = Mock()
    message = LidarClusterArray()
    message.header.frame_id = 'map'
    message.header.stamp.sec = 10
    PersonFollowerNode._on_lidar_clusters(follower, message)
    assert not follower._lidar_clusters_received
    assert not follower._obstacle_tracker.mock_calls
    follower._accept_map_target.assert_not_called()
    follower._apply_tracking_motion.assert_not_called()


def test_accepted_delayed_camera_frame_keeps_its_original_observation_time():
    """An allowed delayed frame does not appear newly captured to the estimator."""
    follower = _follower()
    for name in ('_select_target_observation', '_message_stamp_nanoseconds'):
        setattr(follower, name, MethodType(getattr(PersonFollowerNode, name), follower))
    follower._pending_detection = None
    follower._pending_detection_received_s = None
    follower._pending_detection_timer = Mock()
    follower._is_bearing_only = Mock(return_value=False)
    follower._robot_pose = Mock(return_value=(Point2D(0.0, 0.0), 0.0))
    follower._camera_lidar_fusion = Mock(return_value=None)
    follower._camera_estimator.update = Mock(
        return_value=SimpleNamespace(velocity=Point2D(0.0, 0.0)),
    )
    follower._obstacle_tracker = SimpleNamespace(target=None, bind=Mock(return_value=None))
    follower._observed_track_id = 'person-1'
    message = _detections(stamp_s=19)
    message.header.frame_id = 'map'
    message.header.stamp.nanosec = 400_000_000
    PersonFollowerNode._on_detections(follower, message)
    assert follower._last_seen_s == pytest.approx(19.4)
    assert follower._last_camera_seen_s == pytest.approx(19.4)
    assert follower._last_camera_source_stamp_ns == 19_400_000_000
    assert follower._camera_estimator.update.call_args.args[1] == pytest.approx(19.4)
    follower._accept_map_target.assert_called_once()
    assert follower._accept_map_target.call_args.kwargs[
        'source_stamp_ns'] == 19_400_000_000


@pytest.mark.parametrize('age_s, accepted', [
    (0.0, True), (0.74, True), (0.76, False), (10.0, False),
    (-0.29, True), (-0.31, False),
])
def test_freshness_reuses_loss_deadline_and_tf_future_slop(age_s, accepted):
    """The existing 0.75 s loss policy also bounds accepted observation age."""
    follower = _follower()
    stamp_ns = round((20.0 - age_s) * 1_000_000_000)
    assert follower._observation_is_current(stamp_ns, 20.0) is accepted


def test_unstamped_legacy_observation_retains_existing_compatibility():
    """Only explicitly stamped old observations are discarded by the age gate."""
    follower = _follower()
    assert follower._observation_is_current(0, 20.0)
    assert follower._observation_is_current(None, 20.0)


def test_pending_tf_retry_expires_by_source_age_not_recent_receipt():
    """A queued image cannot remain current merely because it arrived late."""
    follower = _follower()
    follower._pending_detection = _detections(stamp_s=19)
    follower._pending_detection_received_s = 19.99
    follower._pending_detection_timer = Mock()
    follower._record_camera_miss = Mock()
    follower._on_detections = Mock()
    PersonFollowerNode._retry_pending_detection(follower, 20.0)
    assert follower._pending_detection is None
    assert follower._pending_detection_received_s is None
    follower._pending_detection_timer.cancel.assert_called_once_with()
    follower._record_camera_miss.assert_called_once_with()
    follower._on_detections.assert_not_called()


def test_new_camera_frame_replaces_the_pending_tf_frame():
    """A missing transform retains the newest image, not the first waiting one."""
    follower = _follower()
    follower._pending_detection = _detections(stamp_s=19)
    follower._pending_detection_received_s = 19.9
    follower._pending_detection_timer = Mock()
    follower._select_target_observation = Mock()

    def missing_tf(_message):
        follower._detection_transform_pending = True
        return None

    follower._select_target_observation.side_effect = missing_tf
    newest = _detections(stamp_s=20)
    PersonFollowerNode._on_detections(follower, newest)
    assert follower._pending_detection is newest
    assert follower._pending_detection_received_s == 20.0
    follower._now_seconds.return_value = 20.02
    PersonFollowerNode._on_detections(follower, newest, retrying_transform=True)
    assert follower._pending_detection_received_s == 20.0
    follower._apply_tracking_motion.assert_not_called()


@pytest.mark.parametrize('age_s, remaining_ns', [
    (0.0, 750_000_000), (0.6, 150_000_000), (1.0, 1),
])
def test_loss_timer_uses_remaining_sensor_deadline(age_s, remaining_ns):
    """A 600 ms delayed observation leaves 150 ms, not another full 750 ms."""
    follower = _follower()
    follower._last_seen_s = 20.0 - age_s
    follower._settings = SimpleNamespace(observation_loss_debounce_s=0.75)
    follower._loss_timer = Mock()
    PersonFollowerNode._arm_loss_timer(follower)
    assert abs(follower._loss_timer.timer_period_ns - remaining_ns) <= 1
    follower._loss_timer.reset.assert_called_once_with()


def test_early_loss_timer_only_rearms_the_remaining_deadline():
    """An early timer wake does not restart the entire observation grace period."""
    follower = _follower()
    follower._last_seen_s = 19.4
    follower._settings = SimpleNamespace(observation_loss_debounce_s=0.75)
    follower._loss_timer = Mock()
    follower._begin_loss_recovery = Mock()
    follower._arm_loss_timer = MethodType(PersonFollowerNode._arm_loss_timer, follower)
    PersonFollowerNode._on_loss_timer(follower)
    assert abs(follower._loss_timer.timer_period_ns - 150_000_000) <= 1
    follower._loss_timer.reset.assert_called_once_with()
    follower._begin_loss_recovery.assert_not_called()


def test_real_follower_uses_latest_only_sensor_subscriptions(monkeypatch):
    """An isolated, idle node cannot accumulate old camera/LiDAR DDS samples."""
    monkeypatch.setenv('ROS_LOCALHOST_ONLY', '1')
    context = Context()
    rclpy.init(context=context, domain_id=197)
    monkeypatch.setattr('rclpy.node.get_default_context', lambda: context)
    follower = None
    try:
        follower = PersonFollowerNode()
        assert follower.context is context
        assert follower._detection_subscription.qos_profile.depth == 1
        assert follower._lidar_clusters_subscription.qos_profile.depth == 1
        assert follower._active_goal is None
        assert follower._loss_timer.is_canceled()
        assert follower._pending_detection_timer.is_canceled()
    finally:
        if follower is not None:
            follower.destroy_node()
        context.shutdown()
