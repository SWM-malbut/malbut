"""Exercise AutoSlam through isolated ROS endpoints without a robot or simulator."""

from concurrent.futures import Future
import math
import os
from pathlib import Path
from threading import Event, Thread
import time
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

from action_msgs.msg import GoalStatus
from geometry_msgs.msg import PoseStamped, TransformStamped, Twist
from lifecycle_msgs.msg import State
from malbut_interfaces.action import AutoSlam
from nav2_msgs.action import ComputePathToPose, NavigateToPose
from nav2_msgs.srv import SaveMap
from nav_msgs.msg import OccupancyGrid, Odometry
import pytest
import rclpy
from rclpy.action import ActionClient, ActionServer, CancelResponse
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.context import Context
from rclpy.duration import Duration
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.parameter import Parameter
from rclpy.qos import DurabilityPolicy, QoSProfile
from tf2_ros import TransformBroadcaster

from malbut_autoslam.autoslam_node import AutoSlamNode, Navigation, map_base
from malbut_autoslam.runtime import RuntimeGraph


TIMEOUT_S = 6.0


def _wait_until(predicate):
    deadline = time.monotonic() + TIMEOUT_S
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError('isolated test condition did not become true')


def _result(future):
    _wait_until(future.done)
    return future.result()


class _Backend(Node):
    """Provide synthetic map/TF and inert Nav2/save endpoints in a private context."""

    def __init__(self, context, prefix, directory, scene, navigation,
                 planning, navigation_succeeds):
        super().__init__('autoslam_test_backend', context=context)
        self.directory = directory
        self.scene = scene
        self.started = Event()
        self.cancel_seen = Event()
        self.release_cancel = Event()
        self.stopping = Event()
        self.save_requests = []
        self.planning_requests = []
        self.navigation_requests = []
        self.planning_mode = planning
        self.navigation_succeeds = navigation_succeeds
        self.navigation_active = Event()
        self.group = ReentrantCallbackGroup()
        self.publisher = self.create_publisher(
            OccupancyGrid, prefix + '/map',
            QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL))
        self.command_publisher = self.create_publisher(Twist, prefix + '/cmd_vel', 10)
        self.broadcaster = TransformBroadcaster(self)
        self.create_timer(0.05, self._publish)
        self.create_service(
            SaveMap, prefix + '/save_map', self._save, callback_group=self.group)
        self.planner = ActionServer(
            self, ComputePathToPose, prefix + '/plan',
            execute_callback=self._plan,
            cancel_callback=lambda _handle: CancelResponse.ACCEPT,
            callback_group=self.group)
        self.navigation = None
        if navigation:
            self.navigation = ActionServer(
                self, NavigateToPose, prefix + '/navigate',
                execute_callback=self._navigate,
                cancel_callback=lambda _handle: CancelResponse.ACCEPT,
                callback_group=self.group)

    def _publish(self):
        message = OccupancyGrid()
        message.header.frame_id = 'map'
        message.header.stamp = self.get_clock().now().to_msg()
        message.info.width = 50
        message.info.height = 40
        message.info.resolution = 0.1
        message.info.origin.orientation.w = 1.0
        if self.scene == 'frontier':
            message.data = [0 if 10 <= row < 30 and 10 <= col < 30 else -1
                            for row in range(40) for col in range(50)]
        else:
            message.data = [0] * 2000
        self.publisher.publish(message)
        transform = TransformStamped()
        transform.header = message.header
        transform.child_frame_id = 'base_footprint'
        transform.transform.translation.x = 1.5
        transform.transform.translation.y = 2.0
        transform.transform.rotation.w = 1.0
        self.broadcaster.sendTransform(transform)
        if self.navigation_active.is_set():
            command = Twist()
            command.linear.x = 0.3
            self.command_publisher.publish(command)

    def _plan(self, handle):
        self.planning_requests.append(handle.request)
        result = ComputePathToPose.Result()
        if self.planning_mode == 'unreachable':
            handle.abort()
            return result
        start = PoseStamped()
        start.header = handle.request.goal.header
        start.pose.position.x = 1.5
        start.pose.position.y = 2.0
        start.pose.orientation.w = 1.0
        result.path.header = handle.request.goal.header
        result.path.poses = [start, handle.request.goal]
        if self.planning_mode == 'wrong_endpoint':
            result.path.poses = [start]
        elif self.planning_mode == 'unknown_shortcut':
            unknown = PoseStamped()
            unknown.pose.position.x = 0.1
            unknown.pose.position.y = 0.1
            result.path.poses = [start, unknown, handle.request.goal]
        handle.succeed()
        return result

    def _navigate(self, handle):
        self.navigation_requests.append(handle.request)
        self.started.set()
        self.navigation_active.set()
        try:
            if self.navigation_succeeds:
                handle.succeed()
                return NavigateToPose.Result()
            while not handle.is_cancel_requested and not self.stopping.wait(0.01):
                pass
            self.navigation_active.clear()
            if handle.is_cancel_requested:
                self.cancel_seen.set()
                if self.release_cancel.wait(TIMEOUT_S):
                    handle.canceled()
                else:
                    handle.abort()
            else:
                handle.abort()
            return NavigateToPose.Result()
        finally:
            self.navigation_active.clear()
            self.command_publisher.publish(Twist())

    def _save(self, request, response):
        self.save_requests.append(request)
        base = Path(request.map_url)
        assert base.parent == self.directory
        Path(str(base) + '.yaml').write_text('image: home.pgm\n')
        Path(str(base) + '.pgm').write_bytes(b'P5\n1 1\n255\n\xfe')
        response.result = True
        return response


class _System:
    """Own a test-only executor whose requests cannot address real robot actions."""

    def __init__(self, directory, scene='complete', navigation=True,
                 planning='reachable', navigation_succeeds=False, **settings):
        self.context = Context()
        rclpy.init(context=self.context, domain_id=160 + os.getpid() % 30)
        self.prefix = '/test_autoslam_' + uuid4().hex
        self.backend = _Backend(
            self.context, self.prefix, directory, scene, navigation,
            planning, navigation_succeeds)
        defaults = {
            'map_directory': str(directory), 'map_topic': self.prefix + '/map',
            'navigation_action': self.prefix + '/navigate',
            'planning_action': self.prefix + '/plan',
            'save_map_service': self.prefix + '/save_map',
            'cmd_vel_topic': self.prefix + '/cmd_vel',
            'progress_odom_topic': self.prefix + '/odom_rf2o',
            'completion_delay_s': 0.1, 'exploration_period_s': 0.05,
            'ready_timeout_s': 2.0, 'navigation_timeout_s': 4.0,
        }
        defaults.update(settings)
        self.node = AutoSlamNode(
            context=self.context, use_global_arguments=False,
            parameter_overrides=[Parameter(name, value=value)
                                 for name, value in defaults.items()])
        self.client_node = Node('autoslam_test_client', context=self.context)
        self.client = ActionClient(self.client_node, AutoSlam, '/autoslam')
        self.executor = MultiThreadedExecutor(num_threads=6, context=self.context)
        for node in (self.node, self.backend, self.client_node):
            self.executor.add_node(node)
        self.thread = Thread(target=self.executor.spin, daemon=True)
        self.thread.start()

    def ready(self):
        """Wait for discovery only after the fixture owns cleanup of the nodes."""
        assert self.client.wait_for_server(timeout_sec=TIMEOUT_S)
        _wait_until(lambda: self.node.message is not None)

    def request(self, name='home', feedback=None):
        """Send a real AutoSlam goal to the private test server."""
        def callback(event):
            if feedback is not None:
                feedback.append(event.feedback.state)

        return _result(self.client.send_goal_async(
            AutoSlam.Goal(map_name=name), feedback_callback=callback))

    def close(self):
        """Release every mock before stopping the executor, including failed tests."""
        self.node.stopping.set()
        self.node.wake.set()
        self.backend.stopping.set()
        self.backend.release_cancel.set()
        try:
            _wait_until(lambda: not self.node.busy)
        finally:
            self.executor.shutdown(timeout_sec=TIMEOUT_S)
            self.thread.join(TIMEOUT_S)
            self.client.destroy()
            self.node.server.destroy()
            self.backend.planner.destroy()
            if self.backend.navigation is not None:
                self.backend.navigation.destroy()
            for node in (self.node, self.backend, self.client_node):
                node.destroy_node()
            self.context.try_shutdown()


@pytest.fixture
def system_factory(tmp_path, monkeypatch):
    """Constrain test DDS traffic to localhost and always dispose mock servers."""
    monkeypatch.setenv('ROS_LOCALHOST_ONLY', '1')
    systems = []

    def create(**settings):
        system = _System(tmp_path, **settings)
        systems.append(system)
        system.ready()
        return system

    yield create
    for system in reversed(systems):
        system.close()


def test_direct_action_saves_map_after_exploration_finishes(system_factory, tmp_path):
    """Verify Action result and the standard SaveMap request without driving."""
    system = system_factory()
    feedback = []
    handle = system.request(feedback=feedback)
    assert handle.accepted
    result = _result(handle.get_result_async())
    assert result.status == GoalStatus.STATUS_SUCCEEDED
    assert result.result.success
    assert result.result.map_yaml == str(tmp_path / 'home.yaml')
    assert (tmp_path / 'home.pgm').is_file()
    assert 'SAVING' in feedback
    assert not system.backend.started.is_set()
    request = system.backend.save_requests[0]
    assert request.map_topic == system.prefix + '/map'
    assert request.image_format == 'pgm'
    assert request.map_mode == 'trinary'
    assert request.free_thresh == pytest.approx(0.196)
    assert request.occupied_thresh == pytest.approx(0.65)


def test_duplicate_rejected_and_cancel_waits_for_child_terminal(system_factory):
    """Public cancellation must not finish while an accepted Nav2 goal is active."""
    system = system_factory(scene='frontier')
    handle = system.request()
    assert handle.accepted
    assert system.backend.started.wait(TIMEOUT_S)
    assert not system.request('other').accepted
    result_future = handle.get_result_async()
    assert _result(handle.cancel_goal_async()).goals_canceling
    assert system.backend.cancel_seen.wait(TIMEOUT_S)
    assert not result_future.done()
    assert system.node.busy
    system.backend.release_cancel.set()
    result = _result(result_future)
    assert result.status == GoalStatus.STATUS_CANCELED
    assert not result.result.success
    assert system.backend.save_requests == []


def test_navigation_timeout_waits_for_confirmed_stop(system_factory):
    """A timeout requests child cancellation instead of abandoning the goal."""
    system = system_factory(scene='frontier', navigation_timeout_s=0.1)
    handle = system.request()
    result_future = handle.get_result_async()
    assert system.backend.cancel_seen.wait(TIMEOUT_S)
    assert not result_future.done()
    assert system.node.busy
    assert _result(handle.cancel_goal_async()).goals_canceling
    system.backend.release_cancel.set()
    assert _result(result_future).status == GoalStatus.STATUS_CANCELED
    assert system.backend.save_requests == []


def test_stuck_navigation_stops_excludes_and_saves_then_resets(system_factory):
    """No-progress failures survive the retry pass but not a new mapping request."""
    system = system_factory(scene='frontier', progress_timeout_s=0.2)
    handle = system.request()
    result = handle.get_result_async()
    assert system.backend.cancel_seen.wait(TIMEOUT_S)
    assert not result.done()
    assert system.node.busy
    assert system.node.blocked_targets == []  # Wait for terminal, not cancel ACK.
    system.backend.release_cancel.set()
    outcome = _result(result)
    assert outcome.status == GoalStatus.STATUS_SUCCEEDED
    assert outcome.result.success
    targets = [(goal.pose.pose.position.x, goal.pose.pose.position.y)
               for goal in system.backend.navigation_requests]
    assert targets and len(targets) == len(set(targets))
    assert system.node.blocked_approaches
    assert len(system.backend.save_requests) == 1
    # A second run must be allowed to try the same area again.
    count = len(targets)
    second = _result(system.request('second').get_result_async())
    assert second.status == GoalStatus.STATUS_SUCCEEDED
    new = system.backend.navigation_requests[count].pose.pose.position
    assert (new.x, new.y) == targets[0]


@pytest.mark.parametrize('motion', [
    'translation', 'rotation', 'arrival_race', 'no_command', 'turning_while_blocked',
    'source_switch', 'mode_switch',
])
def test_progress_watch_accepts_motion_and_late_success(monkeypatch, motion):
    """Only commanded-axis progress counts; idle waits do not imply an obstacle."""
    clock = [0.0]
    child = SimpleNamespace(handle=SimpleNamespace(accepted=True), error=None, result=None)

    def wait(_seconds):
        clock[0] += 0.2
        if clock[0] >= 2.0:
            child.result = SimpleNamespace(status=GoalStatus.STATUS_SUCCEEDED)
            return True
        return False

    child.done = SimpleNamespace(wait=wait)
    monkeypatch.setattr(time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr('malbut_autoslam.autoslam_node.Navigation', lambda *_args: child)

    def snapshot(with_heading=False):
        assert with_heading
        x = clock[0] * 0.1 if motion == 'translation' else 0.0
        yaw = (math.pi - 0.1 + clock[0] * 0.3
               if motion in ('rotation', 'turning_while_blocked') else 0.0)
        yaw = math.atan2(math.sin(yaw), math.cos(yaw))
        return SimpleNamespace(header=SimpleNamespace(frame_id='map')), (x, 0.0, yaw)

    def settle(_handle):
        child.result = SimpleNamespace(status=GoalStatus.STATUS_SUCCEEDED)
        node.child = None

    def command_mode():
        if motion == 'no_command':
            return None
        if motion == 'rotation' or (motion == 'mode_switch' and 0.9 <= clock[0] < 1.5):
            return 'rotation'
        return 'translation'

    def progress_pose(frame, pose):
        if motion == 'source_switch' and 0.9 <= clock[0] < 1.5:
            return ('odom', 'odom', 'base_footprint'), pose
        return ('tf', frame), pose

    node = SimpleNamespace(
        settings={'navigation_timeout_s': 10.0, 'progress_timeout_s': 0.8,
                  'progress_distance_m': 0.05, 'progress_angle_rad': 0.15},
        navigation=Mock(), _target_pose=Mock(return_value=PoseStamped()),
        _check=Mock(), _snapshot=snapshot, _feedback=Mock(),
        _command_mode=Mock(side_effect=command_mode),
        _progress_pose=Mock(side_effect=progress_pose),
        _settle_child=Mock(side_effect=settle),
        get_logger=Mock(), blocked_targets=[], blocked_approaches=[])
    assert AutoSlamNode._navigate(node, Mock(), Mock(), 'map', run_deadline=10.0)
    assert node._settle_child.call_count == (
        1 if motion in ('arrival_race', 'turning_while_blocked') else 0)
    assert node.blocked_targets == []


def test_command_mode_requires_meaningful_finite_motion(system_factory, monkeypatch):
    """Use both planar axes without making zero or invalid commands a collision."""
    system = system_factory(progress_timeout_s=0.2)
    assert system.node._command_mode() is None
    for vx, vy, angular, expected in (
        (0.0, 0.0, 0.0, None),
        (0.01, 0.0, 0.0, None),
        (0.3, 0.0, 0.0, 'translation'),
        (0.0, -0.3, 0.0, 'translation'),
        (0.0, 0.0, -1.0, 'rotation'),
        (float('nan'), 0.0, 0.0, None),
        (0.0, 0.0, float('inf'), None),
    ):
        command = Twist()
        command.linear.x, command.linear.y = vx, vy
        command.angular.z = angular
        system.node._receive_command(command)
        assert system.node._command_mode() == expected
    command.linear.x = 0.3
    command.angular.z = 0.0
    system.node._receive_command(command)
    expired = time.monotonic() + system.node.settings['sensor_timeout_s'] + 1.0
    monkeypatch.setattr('malbut_autoslam.autoslam_node.time',
                        SimpleNamespace(monotonic=lambda: expired))
    assert system.node._command_mode() is None


@pytest.mark.parametrize('sample', [
    'fresh', 'stale', 'stale_receipt', 'invalid', 'missing_frame',
])
def test_progress_odometry_prefers_only_fresh_valid_measurements(
        system_factory, monkeypatch, sample):
    """Existing RF2O is optional; bad sensor samples fall back to map/TF progress."""
    system = system_factory()
    fallback = (8.0, 9.0, 0.5)
    assert system.node._progress_pose('map', fallback) == (('tf', 'map'), fallback)
    message = Odometry()
    message.header.frame_id = 'odom'
    message.child_frame_id = 'base_footprint'
    stamp = system.node.get_clock().now()
    if sample == 'stale':
        stamp -= Duration(seconds=system.node.settings['sensor_timeout_s'] + 1.0)
    message.header.stamp = stamp.to_msg()
    message.pose.pose.position.x = 1.0
    message.pose.pose.position.y = 2.0
    message.pose.pose.orientation.z = math.sin(0.3 / 2.0)
    message.pose.pose.orientation.w = math.cos(0.3 / 2.0)
    if sample == 'invalid':
        message.pose.pose.position.x = float('nan')
    elif sample == 'missing_frame':
        message.header.frame_id = ''
    system.node._receive_progress_odom(message)
    if sample == 'stale_receipt':
        expired = time.monotonic() + system.node.settings['sensor_timeout_s'] + 1.0
        monkeypatch.setattr('malbut_autoslam.autoslam_node.time',
                            SimpleNamespace(monotonic=lambda: expired))
    source, pose = system.node._progress_pose('map', fallback)
    if sample == 'fresh':
        assert source == ('odom', 'odom', 'base_footprint')
        assert pose == pytest.approx((1.0, 2.0, 0.3))
    else:
        assert (source, pose) == (('tf', 'map'), fallback)


def test_missing_navigation_backend_returns_failure(system_factory):
    """A live map does not make the operation ready without Nav2."""
    system = system_factory(navigation=False, ready_timeout_s=0.15)
    handle = system.request()
    result = _result(handle.get_result_async())
    assert result.status == GoalStatus.STATUS_ABORTED
    assert not result.result.success
    assert 'prerequisites not ready' in result.result.message
    assert not system.backend.save_requests


@pytest.mark.parametrize('planning', ['unreachable', 'wrong_endpoint', 'unknown_shortcut'])
def test_unreachable_frontiers_save_partial_map_without_driving(
        system_factory, tmp_path, planning):
    """A planner failure skips motion and cannot loop forever over the same frontier."""
    system = system_factory(
        scene='frontier', planning=planning, navigation_timeout_s=0.8)
    outcome = _result(system.request().get_result_async())
    assert outcome.status == GoalStatus.STATUS_SUCCEEDED
    assert outcome.result.success
    assert outcome.result.map_yaml == str(tmp_path / 'home.yaml')
    assert outcome.result.known_area_m2 == pytest.approx(4.0)
    assert system.backend.planning_requests
    assert system.backend.navigation_requests == []
    assert len(system.backend.save_requests) == 1
    assert ('Remaining frontiers' in outcome.result.message
            or 'No new mapped space' in outcome.result.message)


def test_navigation_success_without_new_space_finishes_and_saves(system_factory, tmp_path):
    """Nav2 success alone is not mapping progress when fresh maps remain unchanged."""
    system = system_factory(
        scene='frontier', navigation_succeeds=True, navigation_timeout_s=0.8)
    outcome = _result(system.request().get_result_async())
    assert outcome.status == GoalStatus.STATUS_SUCCEEDED
    assert outcome.result.success
    assert outcome.result.map_yaml == str(tmp_path / 'home.yaml')
    assert outcome.result.known_area_m2 == pytest.approx(4.0)
    assert system.backend.navigation_requests
    assert len(system.backend.save_requests) == 1
    assert ('Remaining frontiers' in outcome.result.message
            or 'No new mapped space' in outcome.result.message)


def test_total_exploration_budget_finishes_with_saved_map(system_factory, tmp_path):
    """The hard budget hands off a map even before the longer stagnation limit."""
    system = system_factory(
        scene='frontier', navigation_succeeds=True, max_exploration_time_s=0.1)
    outcome = _result(system.request().get_result_async())
    assert outcome.status == GoalStatus.STATUS_SUCCEEDED
    assert outcome.result.success
    assert 'Exploration time budget reached' in outcome.result.message
    assert outcome.result.map_yaml == str(tmp_path / 'home.yaml')
    assert len(system.backend.save_requests) == 1


@pytest.fixture
def mock_startup(system_factory, monkeypatch):
    """Keep real Action handling but replace every process operation with a mock."""
    runtime = Mock(process=None, log_path=None)
    runtime.components = {}
    monkeypatch.setattr('malbut_autoslam.autoslam_node.OwnedRuntime', lambda _path: runtime)

    def create(**settings):
        system = system_factory(auto_start=True, **settings)
        monkeypatch.setattr(system.node, '_runtime_graph', lambda: RuntimeGraph(
            (), (), (), (), False))
        # The backend's map and TF are real ROS messages, but it has no real
        # hardware or lifecycle nodes. Unit tests cover those readiness checks.
        monkeypatch.setattr(system.node, '_check_sensor_updates', lambda: None)
        monkeypatch.setattr(system.node, '_wait_active_navigation', lambda *_args: None)
        return system, runtime

    return create


def test_action_starts_missing_runtime_and_cleans_it_before_result(mock_startup):
    """A successful public request owns preparation and teardown around exploration."""
    system, runtime = mock_startup()
    handle = system.request()
    result = _result(handle.get_result_async())
    assert result.status == GoalStatus.STATUS_SUCCEEDED
    runtime.acquire.assert_called_once_with()
    runtime.start.assert_called_once()
    assert all(runtime.start.call_args.args[0].values())
    runtime.close.assert_called_once_with()
    assert system.node.runtime is None


def test_startup_failure_cleans_only_owned_runtime(mock_startup):
    """Failed backend startup never sends a navigation Goal or saves a map."""
    system, runtime = mock_startup()
    runtime.start.side_effect = RuntimeError('missing factory launch')
    result = _result(system.request().get_result_async())
    assert result.status == GoalStatus.STATUS_ABORTED
    assert 'missing factory launch' in result.result.message
    runtime.close.assert_called_once_with()
    assert not system.backend.started.is_set()
    assert system.backend.save_requests == []


def test_saved_map_conflict_does_not_start_or_stop_external_nodes(mock_startup, monkeypatch):
    """The active localization mode is never replaced implicitly by mapping."""
    system, runtime = mock_startup()
    monkeypatch.setattr(system.node, '_runtime_graph', lambda: RuntimeGraph(
        ('/amcl',), (), (), (), True))
    result = _result(system.request().get_result_async())
    assert result.status == GoalStatus.STATUS_ABORTED
    assert 'Saved-map localization' in result.result.message
    runtime.start.assert_not_called()
    runtime.close.assert_called_once_with()


def test_cancel_during_backend_readiness_shuts_owned_launch(mock_startup):
    """Cancellation while waiting for Nav2 does not leak the mapping launch."""
    system, runtime = mock_startup(navigation=False)
    handle = system.request()
    _wait_until(lambda: runtime.start.called)
    assert _result(handle.cancel_goal_async()).goals_canceling
    result = _result(handle.get_result_async())
    assert result.status == GoalStatus.STATUS_CANCELED
    runtime.close.assert_called_once_with()
    assert system.backend.save_requests == []


def test_active_external_navigation_goal_is_not_preempted(mock_startup):
    """Direct AutoSLAM startup must not steal a running external Nav2 Goal."""
    system, runtime = mock_startup()
    system.node.navigation_busy = True
    result = _result(system.request().get_result_async())
    assert result.status == GoalStatus.STATUS_ABORTED
    assert 'active goal' in result.result.message
    runtime.start.assert_not_called()
    assert not system.backend.started.is_set()


def test_unconfirmed_runtime_cleanup_holds_action_until_owned_processes_stop(mock_startup):
    """Keep the manager's BASE ownership when runtime termination is uncertain."""
    system, runtime = mock_startup()
    attempted, release = Event(), Event()

    def close():
        attempted.set()
        if not release.is_set():
            raise RuntimeError('owned group still alive')

    runtime.close.side_effect = close
    handle = system.request()
    result = handle.get_result_async()
    try:
        assert attempted.wait(TIMEOUT_S)
        assert not result.done()
        assert system.node.busy
        assert not system.request('other').accepted
    finally:
        release.set()
    assert _result(result).status == GoalStatus.STATUS_SUCCEEDED
    assert system.node.runtime is None


def test_server_stop_during_unanswered_save_still_cleans_owned_mapping(mock_startup, monkeypatch):
    """Parent launch stopping MapSaver cannot leave the owned mapping process orphaned."""
    system, runtime = mock_startup()
    save = Mock(return_value=Future())
    monkeypatch.setattr(system.node.saver, 'call_async', save)
    handle = system.request()
    result = handle.get_result_async()
    _wait_until(lambda: save.called)
    assert not result.done()
    system.node.stopping.set()  # The same flag set by the server's SIGINT handler.
    outcome = _result(result)
    assert outcome.status == GoalStatus.STATUS_ABORTED
    assert not outcome.result.success
    assert 'save result is unconfirmed' in outcome.result.message
    assert outcome.result.map_yaml == ''
    runtime.close.assert_called_once_with()


@pytest.mark.parametrize('transport_error', [False, True])
def test_unanswered_save_times_out_and_rejects_reuse(mock_startup, monkeypatch, transport_error):
    """An uncertain non-cancellable save cleans up but cannot race a later save."""
    system, runtime = mock_startup()
    pending = Future()
    if transport_error:
        pending.set_exception(RuntimeError('response lost'))
    monkeypatch.setattr(system.node.saver, 'call_async', Mock(return_value=pending))
    remove_pending = Mock()
    monkeypatch.setattr(system.node.saver, 'remove_pending_request', remove_pending)
    outcome = _result(system.request().get_result_async())
    assert outcome.status == GoalStatus.STATUS_ABORTED
    assert not outcome.result.success
    assert outcome.result.map_yaml == ''
    assert 'save result is unconfirmed' in outcome.result.message
    if transport_error:
        assert pending.done()
    else:
        assert pending.cancelled()
        remove_pending.assert_called_once_with(pending)
    runtime.close.assert_called_once_with()
    assert system.node.runtime is None
    assert system.node.save_uncertain
    assert not system.node.stopping.is_set()
    assert not system.node.busy
    assert not system.request('other').accepted


def test_live_hardware_readiness_requires_both_sensor_updates(system_factory):
    """Topic discovery alone is not enough to allow motion on the real robot."""
    system = system_factory()
    system.node.settings['auto_start'] = True
    with pytest.raises(RuntimeError, match='fresh LiDAR and odometry'):
        system.node._check_sensor_updates()
    system.node.scan_received_at = time.monotonic()
    with pytest.raises(RuntimeError, match='fresh LiDAR and odometry'):
        system.node._check_sensor_updates()
    system.node.odom_received_at = time.monotonic()
    system.node._check_sensor_updates()


@pytest.mark.parametrize('reply_received', [True, False])
def test_lost_lifecycle_reply_retries_within_startup_deadline(monkeypatch, reply_received):
    """A lost read-only reply neither hangs startup nor bypasses ACTIVE checks."""
    clock = [0.0]
    monkeypatch.setattr(time, 'monotonic', lambda: clock[0])
    pending, retry = Future(), Future()
    if reply_received:
        retry.set_result(SimpleNamespace(current_state=State(id=State.PRIMARY_STATE_ACTIVE)))
    client = Mock()
    client.service_is_ready.return_value = True
    client.call_async.side_effect = [pending, retry]

    def advance():
        clock[0] += 3.0

    node = SimpleNamespace(
        settings={'auto_start': True, 'sensor_timeout_s': 3.0},
        lifecycle_clients={'controller_server': client},
        _check=Mock(), _feedback=Mock(), _pause=advance)
    if reply_received:
        AutoSlamNode._wait_active_navigation(node, Mock(), deadline=6.0)
    else:
        with pytest.raises(RuntimeError, match='controller_server is not active'):
            AutoSlamNode._wait_active_navigation(node, Mock(), deadline=6.0)
        assert retry.cancelled()
    assert pending.cancelled()
    client.remove_pending_request.assert_any_call(pending)
    assert client.call_async.call_count == 2


def test_cancel_before_late_navigation_acceptance_keeps_ownership():
    """Cancel a late-accepted goal once and wait for its actual final result."""
    acceptance, terminal = Future(), Future()
    client = Mock()
    client.send_goal_async.return_value = acceptance
    navigation = Navigation(client, NavigateToPose.Goal())
    navigation.cancel()
    assert not navigation.done.is_set()
    handle = Mock(accepted=True)
    handle.get_result_async.return_value = terminal
    acceptance.set_result(handle)
    navigation.cancel()
    handle.cancel_goal_async.assert_called_once_with()
    assert not navigation.done.is_set()
    final = SimpleNamespace(status=GoalStatus.STATUS_CANCELED)
    terminal.set_result(final)
    assert navigation.done.is_set()
    assert navigation.result is final


def test_navigation_rejection_and_uncertain_transport_are_distinct():
    """Only explicit rejection can safely release a pending child immediately."""
    for rejected in (True, False):
        acceptance = Future()
        client = Mock()
        client.send_goal_async.return_value = acceptance
        navigation = Navigation(client, NavigateToPose.Goal())
        if rejected:
            acceptance.set_result(SimpleNamespace(accepted=False))
            assert navigation.done.is_set()
            assert navigation.error is None
        else:
            acceptance.set_exception(RuntimeError('connection interrupted'))
            assert not navigation.done.is_set()
            assert isinstance(navigation.error, RuntimeError)


@pytest.mark.parametrize('name', ['', '.', '..', '../outside', '/tmp/map', 'a.yaml'])
def test_map_name_cannot_escape_or_replace_a_saved_map(tmp_path, name):
    """Reject invalid stems before any mapping work begins."""
    with pytest.raises(ValueError):
        map_base(tmp_path, name)


def test_saved_map_is_not_overwritten(tmp_path):
    """Existing map files make a new request invalid."""
    (tmp_path / 'home.yaml').write_text('original map')
    with pytest.raises(ValueError, match='already exists'):
        map_base(tmp_path, 'home')


def test_central_manifest_builds_the_actual_action_goal(tmp_path):
    """Use the manager's registry parser against the real registered contract."""
    from malbut_system_manager.manifest_registry import ManifestRegistry

    source = (Path(__file__).parents[2] / 'malbut_interfaces/capabilities'
              / 'autoslam.yaml')
    (tmp_path / 'autoslam.yaml').write_text(source.read_text())
    registry = ManifestRegistry(tmp_path)
    manifest = registry.get('autoslam')
    arguments, goal = registry.parse_arguments(manifest, '{}')
    assert manifest.command_name == '/autoslam'
    assert manifest.command_type == 'malbut_interfaces/action/AutoSlam'
    assert {resource.value for resource in manifest.resources} == {'BASE'}
    assert manifest.execution_mode.value == 'FOREGROUND'
    assert isinstance(goal, AutoSlam.Goal)
    assert arguments == {'map_name': 'home'}
    assert goal.map_name == AutoSlam.Goal().map_name == 'home'


def test_manager_executes_autoslam_and_returns_saved_map(system_factory, tmp_path):
    """Drive the real manager-to-AutoSlam Action chain using only mock backends."""
    from malbut_interfaces.action import ExecuteMission
    from malbut_system_manager.system_manager_node import SystemManagerNode
    import yaml

    system = system_factory()
    manifests = tmp_path / 'capabilities'
    manifests.mkdir()
    source = (Path(__file__).parents[2] / 'malbut_interfaces/capabilities'
              / 'autoslam.yaml')
    (manifests / 'autoslam.yaml').write_text(source.read_text())
    # The existing manager uses the default Context; join the same private
    # localhost domain without changing its production constructor.
    rclpy.init(domain_id=system.context.get_domain_id())
    manager = SystemManagerNode(manifest_directory=str(manifests))
    executor = MultiThreadedExecutor(num_threads=4)
    executor.add_node(manager)
    thread = Thread(target=executor.spin, daemon=True)
    thread.start()
    client = ActionClient(system.client_node, ExecuteMission, '/malbut/mission/execute')
    try:
        assert client.wait_for_server(timeout_sec=TIMEOUT_S)
        request = ExecuteMission.Goal(capability_id='autoslam', arguments_yaml='{}')
        handle = _result(client.send_goal_async(request))
        assert handle.accepted
        result = _result(handle.get_result_async())
        assert result.status == GoalStatus.STATUS_SUCCEEDED
        output = yaml.safe_load(result.result.result_yaml)
        assert output['success'] is True
        assert output['map_yaml'] == str(tmp_path / 'home.yaml')
        assert len(system.backend.save_requests) == 1
        assert not system.backend.started.is_set()
        _wait_until(lambda: manager.downstream_execution_count == 0)
    finally:
        manager.begin_shutdown()
        _wait_until(lambda: manager.downstream_execution_count == 0)
        executor.shutdown(timeout_sec=TIMEOUT_S)
        thread.join(TIMEOUT_S)
        client.destroy()
        manager.destroy_node()
        rclpy.try_shutdown()
