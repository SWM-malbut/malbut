"""Exercise patrol ownership and camera accounting through real ROS Actions."""

import json
import math
import sqlite3
from collections import deque
from threading import Event, RLock, Thread
import time

from action_msgs.msg import GoalStatus
from geometry_msgs.msg import TransformStamped
from map_msgs.msg import OccupancyGridUpdate
from nav2_msgs.action import NavigateToPose, Spin
from nav_msgs.msg import OccupancyGrid
import pytest
import rclpy
from rclpy.action import ActionClient, ActionServer, CancelResponse, GoalResponse
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.parameter import Parameter
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from sensor_msgs.msg import CameraInfo, Image, LaserScan
from std_srvs.srv import SetBool
from tf2_ros import ExtrapolationException, StaticTransformBroadcaster

from malbut_interfaces.action import Patrol
from malbut_patrol.patrol_manager import PatrolManager


TIMEOUT_S = 8.0


def _wait_until(predicate, timeout=TIMEOUT_S):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError('ROS operation did not finish before the test deadline')


def _result(future):
    _wait_until(future.done)
    return future.result()


class _Navigation(Node):
    """Accept Nav2 commands with controllable acceptance and cancellation."""

    def __init__(self):
        super().__init__('patrol_test_navigation')
        self.requests = []
        self.spin_requests = []
        self.on_spin = None
        self.on_navigate = None
        self.request_received = Event()
        self.allow_accept = Event()
        self.allow_accept.set()
        self.cancel_received = Event()
        self.allow_cancel_finish = Event()
        self.allow_cancel_finish.set()
        self.finish_navigation = Event()
        self.abort_navigation = False
        self._closing = Event()
        group = ReentrantCallbackGroup()
        self._navigation = ActionServer(
            self, NavigateToPose, '/patrol_test/navigate_to_pose',
            execute_callback=self._execute_navigation,
            goal_callback=self._accept_navigation,
            cancel_callback=self._cancel,
            callback_group=group,
        )
        self._spin = ActionServer(
            self, Spin, '/patrol_test/spin',
            execute_callback=self._execute_spin,
            cancel_callback=self._cancel,
            callback_group=group,
        )

    def _accept_navigation(self, request):
        self.requests.append(request)
        self.request_received.set()
        if not self.allow_accept.wait(TIMEOUT_S):
            return GoalResponse.REJECT
        return GoalResponse.ACCEPT

    def _cancel(self, _handle):
        self.cancel_received.set()
        return CancelResponse.ACCEPT

    def _execute_navigation(self, handle):
        deadline = time.monotonic() + TIMEOUT_S
        while time.monotonic() < deadline and not self._closing.is_set():
            if handle.is_cancel_requested:
                self.allow_cancel_finish.wait(TIMEOUT_S)
                handle.canceled()
                return NavigateToPose.Result()
            if self.finish_navigation.is_set():
                if self.abort_navigation:
                    handle.abort()
                else:
                    if self.on_navigate is not None:
                        self.on_navigate(handle.request)
                    handle.succeed()
                return NavigateToPose.Result()
            time.sleep(0.01)
        handle.abort()
        return NavigateToPose.Result()

    def _execute_spin(self, handle):
        self.spin_requests.append(handle.request)
        if self.on_spin is not None:
            self.on_spin()
        handle.succeed()
        return Spin.Result()

    def close(self):
        """Release blocking fake operations before shutting down the executor."""
        self._closing.set()
        self.allow_accept.set()
        self.allow_cancel_finish.set()
        self.finish_navigation.set()

    def destroy_node(self):
        """Destroy fake action entities after their callbacks have completed."""
        self._navigation.destroy()
        self._spin.destroy()
        return super().destroy_node()


class _Sensors(Node):
    """Publish a small saved map and calibrated camera without any simulator."""

    def __init__(self):
        super().__init__('patrol_test_sensors')
        durable = QoSProfile(
            depth=1, reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
        )
        self.map = self.create_publisher(
            OccupancyGrid, '/patrol_test/map', durable,
        )
        self.costmap = self.create_publisher(
            OccupancyGrid, '/patrol_test/costmap', durable,
        )
        self.info = self.create_publisher(
            CameraInfo, '/patrol_test/camera_info', 1,
        )
        self.image = self.create_publisher(Image, '/patrol_test/image', 1)
        self.depth = self.create_publisher(Image, '/patrol_test/depth', 1)
        self.scan = self.create_publisher(LaserScan, '/scan', 1)
        self.emit_images = True
        self.depth_delay_frames = 0
        self.pending_depths = deque()
        self.image_frame = 'patrol_test_optical'
        self._transforms = StaticTransformBroadcaster(self)
        robot = TransformStamped()
        robot.header.frame_id = 'map'
        robot.child_frame_id = 'base_footprint'
        robot.transform.translation.x = 1.5
        robot.transform.translation.y = 1.5
        robot.transform.rotation.w = 1.0
        camera = TransformStamped()
        camera.header.frame_id = 'base_footprint'
        camera.child_frame_id = 'patrol_test_camera_link'
        camera.transform.translation.z = 0.5
        camera.transform.rotation.w = 1.0
        optical = TransformStamped()
        optical.header.frame_id = 'patrol_test_camera_link'
        optical.child_frame_id = 'patrol_test_optical'
        optical.transform.rotation.x = -0.5
        optical.transform.rotation.y = 0.5
        optical.transform.rotation.z = -0.5
        optical.transform.rotation.w = 0.5
        self._transforms.sendTransform([robot, camera, optical])
        self._timer = self.create_timer(0.05, self._publish)

    def camera_yaw(self, yaw):
        """Rotate the optical frame while continuing to publish fresh RGB."""
        transform = TransformStamped()
        transform.header.frame_id = 'patrol_test_camera_link'
        transform.child_frame_id = 'patrol_test_optical'
        c, s = math.cos(yaw / 2), math.sin(yaw / 2)
        q = transform.transform.rotation
        q.x, q.y, q.z, q.w = -.5*(c+s), .5*(c-s), .5*(s-c), .5*(c+s)
        self._transforms.sendTransform(transform)

    def move_robot(self, request):
        """Report the navigation endpoint through TF, including its heading."""
        transform = TransformStamped()
        transform.header.frame_id = 'map'
        transform.child_frame_id = 'base_footprint'
        pose = request.pose.pose
        transform.transform.translation.x = pose.position.x
        transform.transform.translation.y = pose.position.y
        transform.transform.rotation = pose.orientation
        self._transforms.sendTransform(transform)
        self.camera_yaw(0)
        time.sleep(.15)

    def _publish(self):
        stamp = self.get_clock().now().to_msg()
        grid = OccupancyGrid()
        grid.header.stamp = stamp
        grid.header.frame_id = 'map'
        grid.info.resolution = 0.25
        grid.info.width = 12
        grid.info.height = 12
        grid.info.origin.orientation.w = 1.0
        grid.data = [
            100 if row in (0, 11) or col in (0, 11) else 0
            for row in range(12) for col in range(12)
        ]
        self.map.publish(grid)
        for row, col in getattr(self, 'live_obstacles', ()):
            grid.data[row * grid.info.width + col] = 100
        self.costmap.publish(grid)
        info = CameraInfo()
        info.header.stamp = stamp
        info.header.frame_id = self.image_frame
        info.width = 2
        info.height = 2
        info.k = [2.0, 0.0, 1.0, 0.0, 2.0, 1.0, 0.0, 0.0, 1.0]
        self.info.publish(info)
        if self.emit_images:
            scan = LaserScan()
            scan.header.stamp = stamp
            scan.header.frame_id = 'patrol_test_camera_link'
            scan.angle_min = -.1
            scan.angle_increment = .1
            scan.range_min, scan.range_max = .03, 8.
            scan.ranges = [1., 1., 1.]
            self.scan.publish(scan)
            image = Image()
            image.header = info.header
            image.width = 2
            image.height = 2
            image.encoding = 'rgb8'
            image.step = 6
            image.data = [0] * 12
            self.image.publish(image)
            depth = Image()
            depth.header = info.header
            depth.width = depth.height = 2
            depth.encoding = '16UC1'
            depth.step = 4
            depth.data = [208, 7] * 4  # 2000 mm, little endian.
            self.pending_depths.append(depth)
            if len(self.pending_depths) > self.depth_delay_frames:
                self.depth.publish(self.pending_depths.popleft())


@pytest.fixture
def patrol_runtime(request, tmp_path):
    """Run the real patrol Action with only external ROS dependencies faked."""
    rclpy.init(domain_id=191)
    sensors = _Sensors()
    navigation = _Navigation()
    values = {
        'map_topic': '/patrol_test/map',
        'costmap_topic': '/patrol_test/costmap',
        'camera_image_topic': '/patrol_test/image',
        'camera_info_topic': '/patrol_test/camera_info',
        'nav2_action_name': '/patrol_test/navigate_to_pose',
        'spin_action_name': '/patrol_test/spin',
        'observation_hz': 20.0,
        'sensor_timeout_s': 3.0,
        'cancel_completion_timeout_s': 3.0,
        'record_observations': True,
        'reobserve_enabled': False,
        'observation_directory': str(tmp_path / 'observations'),
    }
    overrides = dict(getattr(request, 'param', {}))
    sensors.depth_delay_frames = overrides.pop('depth_delay_frames', 0)
    if overrides.pop('inspection_fixture', False):
        calibration = tmp_path / 'synthetic-test-calibration.json'
        calibration.write_text(json.dumps({
            'version': 1, 'validated': True, 'simulated': False,
            'analysis_batch': 'patrol_mosaic_v1',
            'calibration_kind': 'object_presence', 'model': 'offline-fixture',
            'range_m': overrides.pop('inspection_fixture_range', 1.0),
            'camera': {'width': 2, 'height': 2, 'hfov_rad': 2 * math.atan2(2, 4),
                       'height_m': 0.5}}))
        values['inspection_calibration_file'] = str(calibration)
    values.update(overrides)
    manager = PatrolManager(
        parameter_overrides=[Parameter(name, value=value)
                             for name, value in values.items()],
    )
    client_node = Node('patrol_test_client')
    client = ActionClient(client_node, Patrol, '/patrol')
    nodes = (sensors, navigation, manager, client_node)
    executor = MultiThreadedExecutor(num_threads=8)
    for node in nodes:
        executor.add_node(node)
    thread = Thread(target=executor.spin, daemon=True)
    thread.start()
    try:
        assert client.wait_for_server(timeout_sec=TIMEOUT_S)
        _wait_until(lambda: not manager._ready())
        yield manager, navigation, sensors, client
    finally:
        manager.request_shutdown()
        navigation.close()
        executor.shutdown(timeout_sec=TIMEOUT_S)
        thread.join(TIMEOUT_S)
        client.destroy()
        manager.server.destroy()
        manager.navigation.destroy()
        manager.spin_client.destroy()
        for node in reversed(nodes):
            node.destroy_node()
        rclpy.shutdown()


def test_reject_invalid_and_duplicate_goals(patrol_runtime):
    """Reject invalid levels and concurrent requests without replacing motion."""
    manager, navigation, _sensors, client = patrol_runtime
    invalid = Patrol.Goal()
    invalid.thoroughness = 99
    assert not _result(client.send_goal_async(invalid)).accepted
    assert not navigation.requests

    handle = _result(client.send_goal_async(Patrol.Goal()))
    assert handle.accepted
    assert navigation.request_received.wait(TIMEOUT_S)
    assert not _result(client.send_goal_async(Patrol.Goal())).accepted
    assert len(navigation.requests) == 1

    assert _result(handle.cancel_goal_async()).goals_canceling
    outcome = _result(handle.get_result_async())
    assert outcome.status == GoalStatus.STATUS_CANCELED
    assert not outcome.result.success
    assert navigation.cancel_received.is_set()
    assert not manager.busy


def test_processing_cycles_keep_server_alive_and_idle_buffers_empty(patrol_runtime, monkeypatch):
    """Streaming sensor callbacks must survive repeated runtime sleep/wake RPCs."""
    manager, navigation, sensors, client = patrol_runtime
    processing = sensors.create_client(SetBool, '/patrol_manager/set_processing')
    assert processing.wait_for_service(timeout_sec=TIMEOUT_S)
    selected, release = Event(), Event()
    subscription = manager._image_subscription
    take = manager.executor._take_subscription

    def paused_take(entity):
        if entity is subscription and not selected.is_set():
            selected.set()
            assert release.wait(TIMEOUT_S)
        return take(entity)

    # Reproduce the robot's interleaving: an executor worker selected the RGB
    # subscription before the processing service requests its destruction.
    monkeypatch.setattr(manager.executor, '_take_subscription', paused_take)
    try:
        assert selected.wait(TIMEOUT_S)
        assert _result(processing.call_async(SetBool.Request(data=False))).success
        release.set()
        for _ in range(30):
            assert _result(processing.call_async(SetBool.Request(data=False))).success
            time.sleep(.06)
            assert manager.image is None and manager.costmap is not None
            assert not manager.rgb_images and not manager.depth_images
            assert not manager.lidar_scans
            assert client.server_is_ready()
            assert _result(processing.call_async(SetBool.Request(data=True))).success
            assert not manager._ready()
        assert not navigation.requests and not navigation.spin_requests
        handle = _result(client.send_goal_async(Patrol.Goal()))
        assert handle.accepted
        assert navigation.request_received.wait(TIMEOUT_S)
        assert _result(handle.cancel_goal_async()).goals_canceling
        assert _result(handle.get_result_async()).status == GoalStatus.STATUS_CANCELED
    finally:
        release.set()
        sensors.destroy_client(processing)


def test_arrival_rechecks_turning_space_before_sending_spin(patrol_runtime):
    """A new obstacle beside a free centre prevents a turn and invalidates the spot."""
    manager, navigation, sensors, _client = patrol_runtime
    manager.return_pose = manager._robot_pose()
    manager._refresh_maneuvers()
    assert manager._observation_allowed(*manager._robot_xy())
    # The robot is in (6, 6); its centre stays free but its swept body is blocked.
    sensors.live_obstacles = [(5, 6)]
    _wait_until(lambda: manager.costmap.data[5 * 12 + 6] == 100)
    assert manager.costmap.data[6 * 12 + 6] == 0
    assert not manager._spin(None)
    assert not navigation.spin_requests
    assert not manager._observation_allowed(*manager._robot_xy())


@pytest.mark.parametrize('patrol_runtime', [{'reobserve_enabled': True}], indirect=True)
def test_reobservation_analysis_wait_remains_cancellable(patrol_runtime, monkeypatch):
    """A missing Agent must never hold patrol ownership after a user's cancellation."""
    from malbut_patrol.coverage import CoveragePlanner

    monkeypatch.setattr(CoveragePlanner, 'complete', property(lambda self: True))
    manager, navigation, _sensors, client = patrol_runtime
    handle = _result(client.send_goal_async(Patrol.Goal()))
    assert handle.accepted
    _wait_until(lambda: manager.phase == 'ANALYZING')
    assert not navigation.requests
    assert _result(handle.cancel_goal_async()).goals_canceling
    assert _result(handle.get_result_async()).status == GoalStatus.STATUS_CANCELED
    assert not manager.busy


@pytest.mark.parametrize('patrol_runtime', [{
    'reobserve_enabled': True, 'reobserve_analysis_timeout_s': .3,
}], indirect=True)
def test_missing_analysis_has_bounded_wait_and_does_not_invent_retry(patrol_runtime, monkeypatch):
    """Leave photographs for the Agent and report timeout without issuing another movement."""
    from malbut_patrol.coverage import CoveragePlanner

    monkeypatch.setattr(CoveragePlanner, 'complete', property(lambda self: True))
    manager, navigation, _sensors, client = patrol_runtime
    handle = _result(client.send_goal_async(Patrol.Goal()))
    outcome = _result(handle.get_result_async())
    assert outcome.status == GoalStatus.STATUS_SUCCEEDED
    assert manager.reobserve_status['state'] == 'analysis_timeout'
    assert manager.reobserve_status['views'] == 0
    assert not navigation.requests and not manager.busy


@pytest.mark.parametrize('arrives', [True, False])
def test_return_preserves_start_and_requires_measured_arrival(
        patrol_runtime, monkeypatch, arrives):
    """A permissive Nav2 success must not claim return while the robot is still away."""
    from malbut_patrol.coverage import CoveragePlanner

    monkeypatch.setattr(CoveragePlanner, 'complete', property(lambda self: True))
    manager, navigation, _sensors, client = patrol_runtime
    current = [2.1, 1.5]
    monkeypatch.setattr(manager, '_robot_xy', lambda: tuple(current))
    handle = _result(client.send_goal_async(Patrol.Goal()))
    assert navigation.request_received.wait(TIMEOUT_S)
    assert manager.phase == 'RETURNING'
    assert manager.return_pose[:2] == (1.5, 1.5)
    goal = navigation.requests[-1].pose.pose.position
    assert (goal.x, goal.y) == (1.5, 1.5)
    assert manager.route_status['remaining'][-1] == {
        'kind': 'return', 'x': 1.5, 'y': 1.5}
    if arrives:
        current[:] = [1.6, 1.5]
    navigation.finish_navigation.set()
    outcome = _result(handle.get_result_async())
    assert outcome.result.success is arrives
    assert outcome.status == (GoalStatus.STATUS_SUCCEEDED if arrives
                              else GoalStatus.STATUS_ABORTED)
    assert manager.return_status['state'] == ('arrived' if arrives else 'outside_tolerance')


def test_cancel_during_return_stops_motion_and_does_not_restart(patrol_runtime, monkeypatch):
    """Cancellation means stop; it must never spawn another automatic return goal."""
    from malbut_patrol.coverage import CoveragePlanner

    monkeypatch.setattr(CoveragePlanner, 'complete', property(lambda self: True))
    manager, navigation, _sensors, client = patrol_runtime
    monkeypatch.setattr(manager, '_robot_xy', lambda: (2.1, 1.5))
    handle = _result(client.send_goal_async(Patrol.Goal()))
    assert navigation.request_received.wait(TIMEOUT_S)
    assert manager.phase == 'RETURNING'
    assert _result(handle.cancel_goal_async()).goals_canceling
    assert _result(handle.get_result_async()).status == GoalStatus.STATUS_CANCELED
    assert navigation.cancel_received.is_set()
    assert len(navigation.requests) == 1 and not manager.busy


def test_blocked_start_is_reported_as_failed_return(patrol_runtime, monkeypatch):
    """A newly blocked home position must not be dispatched or marked completed."""
    from malbut_patrol.coverage import CoveragePlanner

    monkeypatch.setattr(CoveragePlanner, 'complete', property(lambda self: True))
    manager, navigation, _sensors, client = patrol_runtime
    monkeypatch.setattr(manager, '_robot_xy', lambda: (2.1, 1.5))
    monkeypatch.setattr(manager, '_allowed', lambda x, y: False)
    handle = _result(client.send_goal_async(Patrol.Goal()))
    outcome = _result(handle.get_result_async())
    assert outcome.status == GoalStatus.STATUS_ABORTED
    assert manager.return_status['state'] == 'blocked'
    assert not navigation.requests


@pytest.mark.parametrize('patrol_runtime', [{'reobserve_enabled': True}], indirect=True)
def test_reobservation_uses_nav2_and_saves_a_linked_new_photo(patrol_runtime, monkeypatch):
    """Exercise the Action transition from analyzed unknown to a fresh, separate capture."""
    from malbut_patrol.coverage import CoveragePlanner

    monkeypatch.setattr(CoveragePlanner, 'complete', property(lambda self: True))
    manager, navigation, _sensors, client = patrol_runtime
    root = []

    def feedback(directory, patrol_id, map_id, simulated):
        if not root:
            root.append(patrol_id)
        if patrol_id != root[0]:
            return [({'captured_at': '2026-10-23'}, {'objects': []})]
        files = list((directory / 'captures').glob('[a-f0-9]'*32+'.json'))
        metadata = json.loads(files[0].read_text())
        return [(metadata, {'objects': [{'name': '상자', 'kind': 'object', 'confidence': 'high',
                                        'spatial': {'status': 'unknown', 'reobserve_hint': {
                                            'xy': [2., 1.5],
                                            'purpose': 'aim_only_not_object_position'}}}]})]

    monkeypatch.setattr('malbut_patrol.patrol_manager.read_results', feedback)
    # Navigation can succeed with any yaw. The first camera pose faces away.
    monkeypatch.setattr(manager, '_reobserve_heading_error',
                        lambda target, not_before: 0.0 if navigation.spin_requests else 1.2)
    navigation.finish_navigation.set()
    handle = _result(client.send_goal_async(Patrol.Goal()))
    outcome = _result(handle.get_result_async())
    assert outcome.status == GoalStatus.STATUS_SUCCEEDED
    assert len(navigation.requests) == 1
    assert len(navigation.spin_requests) == 1
    assert navigation.spin_requests[0].target_yaw == pytest.approx(1.2)
    assert manager.reobserve_status['views'] == 1
    assert manager.reobserve_status['state'] == 'no_actionable_unknowns'
    files = list((manager.photo_recorder.directory / 'captures').glob('[a-f0-9]'*32+'.json'))
    assert len(files) == 2
    records = [json.loads(p.read_text()) for p in files]
    original = next(m for m in records if 'reobserve_of' not in m)
    retry = next(m for m in records if 'reobserve_of' in m)
    assert retry['reobserve_of'] == original['patrol_id']
    assert retry['reobserve_target']['observation_id'] == original['observation_id']


@pytest.mark.parametrize('patrol_runtime', [
    {'depth_aligned_to_rgb': True, 'depth_image_topic': '/patrol_test/depth',
     'depth_delay_frames': frames} for frames in (0, 3)
], indirect=True)
def test_depth_subscriptions_follow_processing_and_capture_evidence(patrol_runtime):
    """Discard idle depth input and preserve a synchronized pair after wake."""
    manager, navigation, sensors, client = patrol_runtime
    _wait_until(lambda: bool(manager.depth_images) and bool(manager.lidar_scans))
    manager._change_processing(False)
    assert not manager.depth_images
    assert not manager.lidar_scans
    manager._change_processing(True)
    _wait_until(lambda: not manager._ready() and bool(manager.depth_images)
                and bool(manager.lidar_scans))
    assert manager._depth_subscription is not None
    assert manager._scan_subscription is not None
    handle = _result(client.send_goal_async(Patrol.Goal()))
    assert handle.accepted
    assert navigation.request_received.wait(TIMEOUT_S)
    time.sleep(.3)
    assert manager.photo_recorder.status()['queued'] == 0
    navigation.on_navigate = sensors.move_robot
    navigation.finish_navigation.set()
    _wait_until(lambda: manager.photo_recorder.status().get('saved') == 1)
    manifest, = (manager.photo_recorder.directory / 'captures').glob('[a-f0-9]' * 32 + '.json')
    metadata = json.loads(manifest.read_text())
    assert metadata['spatial']['status'] == 'available'
    assert metadata['spatial']['depth_shape'] == [2, 2]
    stamp = metadata['image_stamp']
    assert stamp['sec'] + stamp['nanosec']/1e9 >= manager.capture_not_before
    depth_stamp = metadata['spatial']['depth_stamp']
    assert abs(stamp['sec']-depth_stamp['sec'] +
               (stamp['nanosec']-depth_stamp['nanosec'])/1e9) <= .05
    assert manifest.with_name(manifest.stem + '.depth.npy').exists()
    assert metadata['lidar']['status'] == 'available'
    assert manifest.with_name(manifest.stem + '.lidar.npy').exists()
    assert _result(handle.cancel_goal_async()).goals_canceling
    assert _result(handle.get_result_async()).status == GoalStatus.STATUS_CANCELED


def test_photo_writer_cannot_delay_ros_cancellation(patrol_runtime):
    """Save real camera evidence while a blocked disk writer leaves Nav2 cancellable."""
    manager, navigation, _sensors, client = patrol_runtime
    entered, release = Event(), Event()
    recorder = manager.photo_recorder
    original = recorder._save

    def delayed_save(image, metadata):
        entered.set()
        assert release.wait(TIMEOUT_S)
        original(image, metadata)

    recorder._save = delayed_save
    try:
        handle = _result(client.send_goal_async(Patrol.Goal()))
        assert handle.accepted and entered.wait(TIMEOUT_S)
        assert navigation.request_received.wait(TIMEOUT_S)
        assert _result(handle.cancel_goal_async()).goals_canceling
        result = _result(handle.get_result_async())
        assert result.status == GoalStatus.STATUS_CANCELED
        assert not manager.busy and recorder.status()['saved'] == 0
    finally:
        release.set()
    _wait_until(lambda: recorder.status()['saved'] == 1)
    manifest, = list((recorder.directory / 'captures').glob('[a-f0-9]' * 32 + '.json'))
    metadata = json.loads(manifest.read_text())
    assert manifest.with_suffix('.jpg').exists()
    assert metadata['camera_pose']['x'] == 1.5
    assert metadata['camera_pose']['y'] == 1.5
    assert metadata['camera_frame'] == 'patrol_test_optical'
    assert metadata['image_stamp']['sec'] > 0
    _wait_until(lambda: recorder.status()['batch_ready'])
    closed_path = recorder.directory / 'runs' / (metadata['patrol_id'] + '.json')
    closed = json.loads(closed_path.read_text())
    assert closed['outcome'] == 'canceled'
    assert [item['observation_id'] for item in closed['captures']] == [manifest.stem]


def test_cancel_before_child_acceptance_waits_for_child_stop(patrol_runtime):
    """Keep ownership through delayed acceptance and actual Nav2 cancellation."""
    manager, navigation, _sensors, client = patrol_runtime
    navigation.allow_accept.clear()
    navigation.allow_cancel_finish.clear()
    handle = _result(client.send_goal_async(Patrol.Goal()))
    assert handle.accepted
    assert navigation.request_received.wait(TIMEOUT_S)
    outcome_future = handle.get_result_async()

    assert _result(handle.cancel_goal_async()).goals_canceling
    assert not outcome_future.done()
    assert manager.busy
    assert not _result(client.send_goal_async(Patrol.Goal())).accepted

    navigation.allow_accept.set()
    assert navigation.cancel_received.wait(TIMEOUT_S)
    # Cancellation acknowledgement alone must not release patrol ownership.
    time.sleep(0.1)
    assert not outcome_future.done()
    assert manager.busy
    navigation.allow_cancel_finish.set()
    outcome = _result(outcome_future)
    assert outcome.status == GoalStatus.STATUS_CANCELED
    assert not manager.busy


@pytest.mark.parametrize('patrol_runtime', [
    {'cancel_completion_timeout_s': 0.1},
], indirect=True)
def test_cancel_watchdog_does_not_release_unconfirmed_motion(patrol_runtime):
    """An elapsed warning threshold cannot finish an unconfirmed cancellation."""
    manager, navigation, _sensors, client = patrol_runtime
    navigation.allow_cancel_finish.clear()
    handle = _result(client.send_goal_async(Patrol.Goal()))
    assert handle.accepted
    assert navigation.request_received.wait(TIMEOUT_S)
    outcome_future = handle.get_result_async()
    assert _result(handle.cancel_goal_async()).goals_canceling
    assert navigation.cancel_received.wait(TIMEOUT_S)
    time.sleep(0.25)
    assert not outcome_future.done()
    assert manager.busy
    assert not _result(client.send_goal_async(Patrol.Goal())).accepted

    navigation.allow_cancel_finish.set()
    assert _result(outcome_future).status == GoalStatus.STATUS_CANCELED
    assert not manager.busy


@pytest.mark.parametrize('patrol_runtime', [
    {'camera_optical_frame': 'patrol_test_optical'},
], indirect=True)
def test_body_frame_camera_headers_use_configured_optical_tf(patrol_runtime):
    """Use optical viewing axes when a bridge labels images with camera_link."""
    manager, navigation, sensors, client = patrol_runtime
    sensors.image_frame = 'patrol_test_camera_link'
    _wait_until(lambda: manager.image.header.frame_id == sensors.image_frame)
    handle = _result(client.send_goal_async(Patrol.Goal()))
    assert handle.accepted
    assert navigation.request_received.wait(TIMEOUT_S)
    _wait_until(lambda: manager.planner.coverage_ratio > 0.0)
    assert _result(handle.cancel_goal_async()).goals_canceling
    assert _result(handle.get_result_async()).status == GoalStatus.STATUS_CANCELED


def test_nav2_success_without_new_rgb_does_not_credit_coverage(patrol_runtime):
    """Neither arriving nor spinning manufactures camera observations."""
    manager, navigation, sensors, client = patrol_runtime
    handle = _result(client.send_goal_async(Patrol.Goal()))
    assert handle.accepted
    assert navigation.request_received.wait(TIMEOUT_S)
    _wait_until(lambda: manager.planner.coverage_ratio > 0.0)
    sensors.emit_images = False
    # Drain the final in-flight frame before establishing the comparison.
    time.sleep(0.2)
    observed = manager.planner.coverage_ratio
    navigation.finish_navigation.set()
    outcome = _result(handle.get_result_async())

    assert outcome.status == GoalStatus.STATUS_ABORTED
    assert not outcome.result.success
    assert outcome.result.viewpoints_visited > 0
    assert outcome.result.coverage_ratio == pytest.approx(observed)
    # Arrival settling can let the live-camera watchdog expire before all
    # viewpoints are exhausted; neither terminal reason may credit new pixels.
    assert ('partial coverage' in outcome.result.message
            or outcome.result.message == 'RGB camera is stale')


@pytest.mark.parametrize('patrol_runtime', [{'sensor_timeout_s': 1.4}], indirect=True)
@pytest.mark.parametrize('delay_s', [.15, .65, .9])
def test_delayed_camera_tf_uses_buffered_frame_without_starving(
        patrol_runtime, monkeypatch, delay_s):
    """Continuous fresh RGB must not discard every frame before its TF arrives."""
    manager, navigation, _sensors, client = patrol_runtime
    lookup = manager.tf.lookup_transform

    def delayed(target, source, when, *args, **kwargs):
        if (source == 'patrol_test_optical' and when.nanoseconds
                > manager.get_clock().now().nanoseconds-int(delay_s*1e9)):
            raise ExtrapolationException('Camera frame precedes its TF arrival')
        return lookup(target, source, when, *args, **kwargs)

    monkeypatch.setattr(manager.tf, 'lookup_transform', delayed)
    handle = _result(client.send_goal_async(Patrol.Goal()))
    assert handle.accepted
    assert navigation.request_received.wait(TIMEOUT_S)
    _wait_until(lambda: manager.last_image_stamp is not None)
    stamp = manager.last_image_stamp
    time.sleep(1.6)  # Longer than the unchanged camera watchdog.
    assert manager.busy
    assert manager.last_image_stamp > stamp
    assert manager.planner.coverage_ratio > 0
    assert _result(handle.cancel_goal_async()).goals_canceling
    assert _result(handle.get_result_async()).status == GoalStatus.STATUS_CANCELED


def test_reobservation_heading_waits_for_exact_post_arrival_tf(patrol_runtime, monkeypatch):
    manager, _navigation, _sensors, _client = patrol_runtime
    lookup = manager.tf.lookup_transform
    accepted_stamps = []
    started = manager.get_clock().now().nanoseconds/1e9

    def delayed(target, source, when, *args, **kwargs):
        if when.nanoseconds > manager.get_clock().now().nanoseconds-150_000_000:
            raise ExtrapolationException('TF callback delayed behind RGB')
        accepted_stamps.append(when.nanoseconds/1e9)
        return lookup(target, source, when, *args, **kwargs)

    monkeypatch.setattr(manager.tf, 'lookup_transform', delayed)
    monkeypatch.setattr(manager, '_pump', lambda _: None)
    assert manager._orient_reobservation(None, {'xy': [2.5, 1.5]})
    assert accepted_stamps and min(accepted_stamps) >= started


def test_unreachable_viewpoints_return_partial_failure(patrol_runtime):
    """Exhaust failed goals once and return partial coverage, never success."""
    _manager, navigation, _sensors, client = patrol_runtime
    navigation.abort_navigation = True
    navigation.finish_navigation.set()
    handle = _result(client.send_goal_async(Patrol.Goal()))
    assert handle.accepted
    outcome = _result(handle.get_result_async())

    assert outcome.status == GoalStatus.STATUS_ABORTED
    assert not outcome.result.success
    assert outcome.result.viewpoints_visited == 0
    assert 0.0 < outcome.result.coverage_ratio < 0.9
    assert 'partial coverage' in outcome.result.message
    points = [(goal.pose.pose.position.x, goal.pose.pose.position.y)
              for goal in navigation.requests]
    assert points
    assert len(points) == len(set(points))


@pytest.mark.parametrize('patrol_runtime', [{'stall_timeout_s': 0.3}], indirect=True)
def test_standing_still_moves_on_to_the_next_viewpoint(patrol_runtime):
    """A viewpoint Nav2 keeps retrying without moving the robot is given up early."""
    _manager, navigation, _sensors, client = patrol_runtime
    handle = _result(client.send_goal_async(Patrol.Goal()))
    assert handle.accepted
    _wait_until(lambda: len(navigation.requests) >= 2)  # Long before 120 s.
    assert navigation.cancel_received.is_set()
    assert _result(handle.cancel_goal_async()).goals_canceling
    assert _result(handle.get_result_async()).status == GoalStatus.STATUS_CANCELED


def test_system_manager_uses_registered_patrol_and_cancels_nav2(patrol_runtime):
    """Exercise the installed manifest and both real Action cancellation hops."""
    module = pytest.importorskip(
        'malbut_system_manager.system_manager_node',
        reason='system manager integration requires that package installed',
    )
    from malbut_interfaces.action import ExecuteMission

    manager, navigation, sensors, _client = patrol_runtime
    system = module.SystemManagerNode()
    executor = manager.executor
    executor.add_node(system)
    client = ActionClient(sensors, ExecuteMission, '/malbut/mission/execute')
    try:
        assert client.wait_for_server(timeout_sec=TIMEOUT_S)
        request = ExecuteMission.Goal()
        request.capability_id = 'patrol'
        request.arguments_yaml = '{thoroughness: 1}'
        feedback = []
        handle = _result(client.send_goal_async(
            request, feedback_callback=lambda message: feedback.append(
                message.feedback.state),
        ))
        assert handle.accepted
        assert navigation.request_received.wait(TIMEOUT_S)
        assert manager.busy
        assert _result(handle.cancel_goal_async()).goals_canceling
        outcome = _result(handle.get_result_async())

        assert outcome.status == GoalStatus.STATUS_CANCELED
        assert navigation.cancel_received.is_set()
        assert 'coverage_ratio:' in outcome.result.result_yaml
        assert 'RUNNING' in feedback
        assert not manager.busy
        assert system.downstream_execution_count == 0
    finally:
        system.begin_shutdown()
        _wait_until(lambda: system.downstream_execution_count == 0)
        executor.remove_node(system)
        client.destroy()
        system.destroy_node()


@pytest.mark.parametrize('enabled', [False, True])
def test_costmap_patch_updates_candidate_safety_without_corrupting_grid(enabled):
    """Apply valid occupancy patches and ignore malformed extents atomically."""
    manager = object.__new__(PatrolManager)
    manager._processing_enabled = enabled
    manager.lock = RLock()
    manager.settings = {'maximum_goal_cost': 80}
    manager.costmap_received = 0.0
    grid = OccupancyGrid()
    grid.header.frame_id = 'map'
    grid.info.resolution = 1.0
    grid.info.width = 2
    grid.info.height = 2
    grid.info.origin.orientation.w = 1.0
    grid.data = [0, 100, 0, 0]
    manager.costmap = grid
    assert not manager._allowed(1.5, 0.5)

    update = OccupancyGridUpdate()
    update.header.frame_id = 'map'
    update.header.stamp.sec = 42
    update.x = 1
    update.y = 0
    update.width = 1
    update.height = 1
    update.data = [0]
    manager._receive_costmap_update(update)
    assert manager._allowed(1.5, 0.5)
    assert list(grid.data) == [0, 0, 0, 0]
    assert grid.header.stamp.sec == 42
    assert manager.costmap_received > 0.0

    receipt = manager.costmap_received
    for x, y in ((-1, 0), (0, -1), (2, 0), (0, 2)):
        update = OccupancyGridUpdate()
        update.header.frame_id = 'map'
        update.width = 1
        update.height = 1
        update.x, update.y = x, y
        update.data = [100]
        update.header.stamp.sec = 99
        manager._receive_costmap_update(update)
        assert list(grid.data) == [0, 0, 0, 0]
        assert grid.header.stamp.sec == 42
        assert manager.costmap_received == receipt


def _analyze_inspection_photos(recorder, objects=()):
    # Like the production worker, consume only a completely sealed batch.
    _wait_until(lambda: recorder.status()['batch_ready'])
    _, references = recorder.evidence()
    with sqlite3.connect(recorder.directory / 'observations.sqlite3') as connection:
        connection.executescript('''
            CREATE TABLE IF NOT EXISTS dataset (id INTEGER PRIMARY KEY, simulated INTEGER);
            INSERT OR IGNORE INTO dataset VALUES (1, 0);
            CREATE TABLE IF NOT EXISTS owner (id INTEGER PRIMARY KEY, user_id TEXT);
            INSERT OR IGNORE INTO owner VALUES (1, 'fixture-owner');
            CREATE TABLE IF NOT EXISTS observations (
                id TEXT PRIMARY KEY, user_id TEXT, metadata TEXT, status TEXT,
                model TEXT, error TEXT, analysis TEXT);
        ''')
        for index, reference in enumerate(references):
            identity = reference['observation_id']
            metadata = (recorder.directory / 'captures' / (identity + '.json')).read_text()
            connection.execute('INSERT OR REPLACE INTO observations VALUES (?,?,?,?,?,?,?)',
                               (identity, 'fixture-owner', metadata, 'ready', 'offline-fixture',
                                None, json.dumps({'summary': 'scene',
                                                  'objects': list(objects) if index == 0 else [],
                                                  'uncertainties': []})))


@pytest.mark.parametrize('patrol_runtime', [{'inspection_fixture': True}], indirect=True)
def test_inspection_rotates_first_and_cancel_does_not_wait_for_analysis(patrol_runtime):
    """Keep pending photos uncredited and release a canceled inspection promptly."""
    manager, navigation, _sensors, client = patrol_runtime
    handle = _result(client.send_goal_async(Patrol.Goal()))
    assert handle.accepted
    _wait_until(lambda: manager.phase == 'ANALYZING' and manager.photo_recorder.status()['saved'])
    assert len(navigation.spin_requests) == 1
    assert not navigation.requests
    assert manager.planner.observed_area_m2 == 0
    assert not manager.planner.require_room_entry
    assert _result(handle.cancel_goal_async()).goals_canceling
    result = _result(handle.get_result_async())
    assert result.status == GoalStatus.STATUS_CANCELED
    assert not result.result.success and not manager.busy


@pytest.mark.parametrize('patrol_runtime', [
    {'inspection_fixture': True, 'inspection_fixture_range': 5.0},
], indirect=True)
def test_inspection_completes_only_after_initial_rotation_photos_are_analyzed(patrol_runtime):
    """Exercise real Action completion after a full camera sweep and delayed analysis."""
    manager, navigation, sensors, client = patrol_runtime

    def rotate_camera():
        for step in range(8):
            sensors.camera_yaw(step * math.pi / 4)
            time.sleep(.35)

    navigation.on_spin = rotate_camera
    handle = _result(client.send_goal_async(Patrol.Goal()))
    _wait_until(lambda: manager.phase == 'ANALYZING')
    _wait_until(lambda: manager.photo_recorder.status()['saved']
                == manager.photo_recorder.status()['queued'])
    assert manager.planner.coverage_ratio == 0 and not navigation.requests
    recorder = manager.photo_recorder
    _, references = recorder.evidence()
    assert len(references) >= 8
    _analyze_inspection_photos(recorder)
    result = _result(handle.get_result_async())
    assert result.status == GoalStatus.STATUS_SUCCEEDED and result.result.success
    assert result.result.coverage_ratio >= .95
    assert not navigation.requests and not manager.busy
    assert len(manager.inspection.credited) == len(references)


@pytest.mark.parametrize('patrol_runtime', [{'inspection_fixture': True}], indirect=True)
def test_inspection_photo_limit_with_partial_analysis_is_not_success(patrol_runtime):
    """Stop at the accepted-photo cap even when every saved image was analyzed."""
    manager, navigation, _sensors, client = patrol_runtime
    manager.photo_recorder.max_photos = 1
    handle = _result(client.send_goal_async(Patrol.Goal()))
    _wait_until(lambda: manager.phase == 'ANALYZING' and manager.photo_recorder.status()['saved'])
    _analyze_inspection_photos(manager.photo_recorder)
    result = _result(handle.get_result_async())
    assert result.status == GoalStatus.STATUS_ABORTED
    assert 'photo limit reached' in result.result.message
    assert 0 < result.result.coverage_ratio < .95
    assert not result.result.success and not navigation.requests and not manager.busy


@pytest.mark.parametrize('patrol_runtime', [{'inspection_fixture': True}], indirect=True)
def test_inspection_photo_budget_is_shared_across_viewpoint_batches(patrol_runtime):
    """Starting another spot must not reset the patrol's total basic-photo quota."""
    manager, navigation, sensors, client = patrol_runtime
    recorder = manager.photo_recorder
    recorder.max_photos = 2
    navigation.on_navigate = sensors.move_robot
    navigation.finish_navigation.set()
    handle = _result(client.send_goal_async(Patrol.Goal()))
    _wait_until(lambda: manager.phase == 'ANALYZING' and recorder.status()['batch_ready'])
    first = recorder.identity()['patrol_id']
    _analyze_inspection_photos(recorder)
    _wait_until(lambda: recorder.identity()['patrol_id'] != first
                and manager.phase == 'ANALYZING' and recorder.status()['batch_ready'])
    _analyze_inspection_photos(recorder)
    result = _result(handle.get_result_async())
    assert result.status == GoalStatus.STATUS_ABORTED
    assert 'photo limit reached' in result.result.message
    assert len(manager.inspection.runs) == 2
    assert len(manager.inspection.references) == len(manager.inspection.credited) == 2
    assert manager.return_status['state'] == 'arrived'
    assert len(navigation.requests) == 2  # One coverage goal and the original home.
    assert manager.return_pose[:2] == (1.5, 1.5)
    assert len(list((recorder.directory / 'captures').glob('[a-f0-9]'*32+'.jpg'))) == 2


@pytest.mark.parametrize('patrol_runtime', [{
    'inspection_fixture': True, 'inspection_fixture_range': 5.0, 'reobserve_enabled': True,
}], indirect=True)
def test_analyzed_coverage_still_reobserves_unknowns_then_returns_home(patrol_runtime):
    """Combine calibrated coverage, real DB feedback, retry capture and measured return."""
    manager, navigation, sensors, client = patrol_runtime
    recorder = manager.photo_recorder

    def rotate_camera():
        for step in range(8):
            sensors.camera_yaw(step * math.pi / 4)
            time.sleep(.35)

    navigation.on_spin = rotate_camera
    navigation.on_navigate = sensors.move_robot
    navigation.finish_navigation.set()
    handle = _result(client.send_goal_async(Patrol.Goal()))
    _wait_until(lambda: manager.phase == 'ANALYZING' and recorder.status()['batch_ready'])
    root, originals = recorder.evidence()
    _analyze_inspection_photos(recorder, [{
        'name': '상자', 'kind': 'box', 'confidence': 'high',
        'spatial': {'status': 'unknown', 'reobserve_hint': {
            'xy': [2., 1.5], 'purpose': 'aim_only_not_object_position'}}}])
    _wait_until(lambda: recorder.identity()['patrol_id'] != root['patrol_id']
                and manager.phase == 'ANALYZING' and recorder.status()['batch_ready'])
    assert manager.planner.complete
    assert recorder.status()['saved'] == 1  # Already covered area must allow retry photos.
    _, retry = recorder.evidence()
    path = recorder.directory / 'captures' / (retry[0]['observation_id'] + '.json')
    metadata = json.loads(path.read_text())
    assert metadata['analysis_batch'] == 'patrol_mosaic_v1'
    assert metadata['reobserve_of'] == root['patrol_id']
    assert metadata['reobserve_target']['observation_id'] == originals[0]['observation_id']
    _analyze_inspection_photos(recorder)
    result = _result(handle.get_result_async())
    assert result.status == GoalStatus.STATUS_SUCCEEDED and result.result.success
    assert manager.reobserve_status['views'] == 1
    assert manager.return_status['state'] == 'arrived'
    assert len(navigation.requests) == 2
    assert len(manager.inspection.credited) == len(originals)
    assert set(manager.inspection.runs) == {root['patrol_id']}
    home = navigation.requests[-1].pose.pose.position
    assert (home.x, home.y) == manager.return_pose[:2] == (1.5, 1.5)


@pytest.mark.parametrize('patrol_runtime', [
    {'inspection_fixture': True, 'inspection_analysis_timeout_s': 0.3},
], indirect=True)
def test_inspection_analysis_timeout_is_not_a_success(patrol_runtime):
    """Abort an inspection without an analysis worker instead of crediting RGB."""
    manager, navigation, _sensors, client = patrol_runtime
    handle = _result(client.send_goal_async(Patrol.Goal()))
    result = _result(handle.get_result_async())
    assert result.status == GoalStatus.STATUS_ABORTED
    assert 'analysis timed out' in result.result.message
    assert result.result.coverage_ratio == 0 and not result.result.success
    assert not navigation.requests and not manager.busy


@pytest.mark.parametrize('patrol_runtime', [{'inspection_fixture': True}], indirect=True)
def test_inspection_photo_save_failure_aborts_without_credit(patrol_runtime):
    """Do not turn disk failures into observed space or successful patrols."""
    manager, _navigation, _sensors, client = patrol_runtime

    def fail_save(*_args):
        raise OSError('offline disk failure')

    manager.photo_recorder._save = fail_save
    handle = _result(client.send_goal_async(Patrol.Goal()))
    result = _result(handle.get_result_async())
    assert result.status == GoalStatus.STATUS_ABORTED
    assert 'photo save failed' in result.result.message
    assert not result.result.success and result.result.coverage_ratio == 0
