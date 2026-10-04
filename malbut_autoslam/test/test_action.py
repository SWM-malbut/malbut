"""Exercise AutoSlam through isolated ROS endpoints without a robot or simulator."""

from concurrent.futures import Future
import os
from pathlib import Path
from threading import Event, Thread
import time
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

from action_msgs.msg import GoalStatus
from geometry_msgs.msg import PoseStamped, TransformStamped
from malbut_interfaces.action import AutoSlam
from nav2_msgs.action import ComputePathToPose, NavigateToPose
from nav2_msgs.srv import SaveMap
from nav_msgs.msg import OccupancyGrid
import pytest
import rclpy
from rclpy.action import ActionClient, ActionServer, CancelResponse
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.context import Context
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.parameter import Parameter
from rclpy.qos import DurabilityPolicy, QoSProfile
from std_srvs.srv import Trigger
from tf2_ros import TransformBroadcaster

from malbut_autoslam.autoslam_node import AutoSlamNode, Navigation, map_base


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
                 planning, navigation_succeeds, owned_mapping):
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
        self.mapping_events = []
        self.mapping_started = Event()
        self.release_start = Event()
        self.release_start.set()
        self.mapping_start_success = True
        self.mapping_stop_success = True
        self.group = ReentrantCallbackGroup()
        if owned_mapping:
            self.create_service(
                Trigger, prefix + '/start_mapping', self._start_mapping,
                callback_group=self.group)
            self.create_service(
                Trigger, prefix + '/stop_mapping', self._stop_mapping,
                callback_group=self.group)
        self.publisher = self.create_publisher(
            OccupancyGrid, prefix + '/map',
            QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL))
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

    def _start_mapping(self, _request, response):
        self.mapping_events.append('start')
        self.mapping_started.set()
        assert self.release_start.wait(TIMEOUT_S)
        response.success = self.mapping_start_success
        response.message = 'started' if response.success else 'SLAM startup failed'
        return response

    def _stop_mapping(self, _request, response):
        assert not self.navigation_active.is_set()
        self.mapping_events.append('stop')
        response.success = self.mapping_stop_success
        response.message = 'stopped' if response.success else 'SLAM shutdown failed'
        return response

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
                 planning='reachable', navigation_succeeds=False,
                 owned_mapping=False, **settings):
        self.context = Context()
        rclpy.init(context=self.context, domain_id=160 + os.getpid() % 30)
        self.prefix = '/test_autoslam_' + uuid4().hex
        self.backend = _Backend(
            self.context, self.prefix, directory, scene, navigation,
            planning, navigation_succeeds, owned_mapping)
        defaults = {
            'map_directory': str(directory), 'map_topic': self.prefix + '/map',
            'navigation_action': self.prefix + '/navigate',
            'planning_action': self.prefix + '/plan',
            'save_map_service': self.prefix + '/save_map',
            'completion_delay_s': 0.1, 'exploration_period_s': 0.05,
            'ready_timeout_s': 2.0, 'navigation_timeout_s': 4.0,
        }
        defaults.update(settings)
        if owned_mapping:
            defaults.update(start_mapping_service=self.prefix + '/start_mapping',
                            stop_mapping_service=self.prefix + '/stop_mapping')
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
        self.backend.release_start.set()
        try:
            _wait_until(lambda: not self.node.busy)
        finally:
            assert self.executor.shutdown(timeout_sec=TIMEOUT_S)
            self.thread.join(TIMEOUT_S)
            assert not self.thread.is_alive()
            self.client.destroy()
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


def test_fixture_close_releases_action_waitables(tmp_path, monkeypatch):
    """Do not defer native action handles to a later test's garbage collection."""
    monkeypatch.setenv('ROS_LOCALHOST_ONLY', '1')
    system = _System(tmp_path)
    try:
        system.ready()
    finally:
        system.close()
    assert not list(system.node.waitables)
    assert not system.thread.is_alive()


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


def test_owned_slam_starts_on_request_and_stops_before_success(system_factory):
    """Idle servers do not start SLAM; completed mapping releases its backend."""
    system = system_factory(owned_mapping=True)
    assert system.backend.mapping_events == []
    result = _result(system.request().get_result_async())
    assert result.status == GoalStatus.STATUS_SUCCEEDED
    assert result.result.success
    assert system.backend.mapping_events == ['start', 'stop']
    assert len(system.backend.save_requests) == 1


def test_owned_slam_cancel_waits_for_navigation_before_stopping(system_factory):
    """Do not remove mapping while the child navigation Action can still move."""
    system = system_factory(owned_mapping=True, scene='frontier')
    handle = system.request()
    assert system.backend.started.wait(TIMEOUT_S)
    result = handle.get_result_async()
    assert _result(handle.cancel_goal_async()).goals_canceling
    assert system.backend.cancel_seen.wait(TIMEOUT_S)
    assert not result.done()
    assert system.backend.mapping_events == ['start']
    system.backend.release_cancel.set()
    assert _result(result).status == GoalStatus.STATUS_CANCELED
    assert system.backend.mapping_events == ['start', 'stop']


def test_cancel_during_slam_start_settles_start_then_stops(system_factory):
    """A non-cancellable backend request must not start SLAM after cancellation."""
    system = system_factory(owned_mapping=True)
    system.backend.release_start.clear()
    handle = system.request()
    assert system.backend.mapping_started.wait(TIMEOUT_S)
    result = handle.get_result_async()
    assert _result(handle.cancel_goal_async()).goals_canceling
    assert not result.done()
    system.backend.release_start.set()
    assert _result(result).status == GoalStatus.STATUS_CANCELED
    assert system.backend.mapping_events == ['start', 'stop']
    assert not system.backend.navigation_requests
    assert not system.backend.save_requests


def test_owned_slam_start_failure_still_requests_cleanup(system_factory):
    """Partial backend startup is cleaned up before an aborted result."""
    system = system_factory(owned_mapping=True)
    system.backend.mapping_start_success = False
    result = _result(system.request().get_result_async())
    assert result.status == GoalStatus.STATUS_ABORTED
    assert 'SLAM startup failed' in result.result.message
    assert system.backend.mapping_events == ['start', 'stop']
    assert not system.backend.navigation_requests


def test_owned_slam_cleanup_failure_is_not_reported_as_success(system_factory):
    """An unconfirmed backend shutdown does not silently admit another run."""
    system = system_factory(owned_mapping=True)
    system.backend.mapping_stop_success = False
    result = _result(system.request().get_result_async())
    assert result.status == GoalStatus.STATUS_ABORTED
    assert not result.result.success
    assert 'mapping cleanup failed: SLAM shutdown failed' in result.result.message
    assert not system.request('other').accepted


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


def test_failed_navigation_is_skipped_and_retried_by_a_new_request(system_factory):
    """Nav2 owns obstacle stops; a frontier it cannot reach in time is skipped."""
    system = system_factory(scene='frontier', navigation_timeout_s=0.3)
    handle = system.request()
    result = handle.get_result_async()
    assert system.backend.cancel_seen.wait(TIMEOUT_S)
    assert not result.done()
    assert system.node.busy  # Wait for Nav2's terminal result, not the cancel ACK.
    system.backend.release_cancel.set()
    outcome = _result(result)
    assert outcome.status == GoalStatus.STATUS_SUCCEEDED
    assert outcome.result.success
    targets = [(goal.pose.pose.position.x, goal.pose.pose.position.y)
               for goal in system.backend.navigation_requests]
    assert targets
    assert len(system.backend.save_requests) == 1
    # Failures are forgotten between requests: the next run may try again.
    count = len(targets)
    second = _result(system.request('second').get_result_async())
    assert second.status == GoalStatus.STATUS_SUCCEEDED
    new = system.backend.navigation_requests[count].pose.pose.position
    assert (new.x, new.y) == targets[0]


def test_standing_still_skips_the_frontier_before_the_navigation_timeout(system_factory):
    """A goal Nav2 keeps retrying without moving the robot is given up early."""
    system = system_factory(scene='frontier', navigation_timeout_s=60.0, stall_timeout_s=0.3)
    handle = system.request()
    result = handle.get_result_async()
    assert system.backend.cancel_seen.wait(TIMEOUT_S)  # Long before 60 s.
    assert system.node.busy
    system.backend.release_cancel.set()
    outcome = _result(result)
    assert outcome.status == GoalStatus.STATUS_SUCCEEDED
    assert outcome.result.success


def test_missing_navigation_backend_returns_failure(system_factory):
    """A live map does not make the operation ready without Nav2."""
    system = system_factory(navigation=False, ready_timeout_s=0.15)
    handle = system.request()
    result = _result(handle.get_result_async())
    assert result.status == GoalStatus.STATUS_ABORTED
    assert not result.result.success
    assert 'prerequisites not ready' in result.result.message
    assert not system.backend.save_requests


@pytest.mark.parametrize('changed', ['none', 'new_wall', 'tolerated_endpoint', 'near_wall_start'])
def test_planning_rechecks_goal_margin_on_current_map_without_trapping_start(monkeypatch, changed):
    """Fresh goal clearance is mandatory, but not 30cm padding on every path cell."""
    message = OccupancyGrid()
    message.header.frame_id = 'map'
    message.info.width = message.info.height = 40
    message.info.resolution = 0.05
    message.info.origin.orientation.w = 1.0
    message.data = [0] * 1600
    frontier = SimpleNamespace(x=1.0, y=1.0)
    endpoint = PoseStamped()
    endpoint.pose.position.x = 1.0
    endpoint.pose.position.y = 1.0
    robot = (0.5, 1.0)
    if changed == 'new_wall':
        # The target remains in a free cell, but a new nearby occupied cell
        # discovered during planning invalidates its earlier 30cm margin.
        message.data[24 * 40 + 20] = 100
    elif changed == 'tolerated_endpoint':
        endpoint.pose.position.y = 1.25
        message.data[29 * 40 + 20] = 100
    elif changed == 'near_wall_start':
        robot = (0.2, 1.0)
    planning = SimpleNamespace(
        done=SimpleNamespace(wait=lambda _timeout: True), cancel=Mock(),
        result=SimpleNamespace(status=GoalStatus.STATUS_SUCCEEDED, result=SimpleNamespace(
            path=SimpleNamespace(header=SimpleNamespace(frame_id='map'), poses=[endpoint]))))
    monkeypatch.setattr('malbut_autoslam.autoslam_node.Navigation', lambda *_args: planning)
    node = SimpleNamespace(
        settings={'ready_timeout_s': 1.0, 'robot_clearance_m': 0.30},
        planner=Mock(), _target_pose=Mock(return_value=PoseStamped()),
        _check=Mock(), _snapshot=Mock(return_value=(message, robot)))
    assert AutoSlamNode._can_reach(node, Mock(), frontier, 'map') == (
        changed in ('none', 'near_wall_start'))
    planning.cancel.assert_called_once()
    if changed in ('new_wall', 'tolerated_endpoint'):
        assert node.planned_path == []


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
    assert all(request.planner_id == 'GridBased'
               for request in system.backend.planning_requests)
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


def test_server_stop_during_unanswered_save_aborts_without_a_map(system_factory, monkeypatch):
    """Parent launch stopping MapSaver ends the request without claiming a saved map."""
    system = system_factory()
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


@pytest.mark.parametrize('transport_error', [False, True])
def test_unanswered_save_times_out_and_rejects_reuse(system_factory, monkeypatch, transport_error):
    """An uncertain non-cancellable save ends the request but cannot race a later save."""
    system = system_factory()
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
    assert system.node.save_uncertain
    assert not system.node.stopping.is_set()
    assert not system.node.busy
    assert not system.request('other').accepted


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


@pytest.mark.parametrize('owned_mapping', [False, True])
def test_manager_executes_autoslam_and_returns_saved_map(system_factory, tmp_path, owned_mapping):
    """Drive the real manager-to-AutoSlam Action chain using only mock backends."""
    from malbut_interfaces.action import ExecuteMission
    from malbut_system_manager.models import LocalizationMode
    from malbut_system_manager.system_manager_node import SystemManagerNode
    import yaml

    system = system_factory(owned_mapping=owned_mapping)
    manifests = tmp_path / 'capabilities'
    manifests.mkdir()
    source = (Path(__file__).parents[2] / 'malbut_interfaces/capabilities'
              / 'autoslam.yaml')
    (manifests / 'autoslam.yaml').write_text(source.read_text())
    # The existing manager uses the default Context; join the same private
    # localhost domain without changing its production constructor.
    rclpy.init(domain_id=system.context.get_domain_id())
    manager = SystemManagerNode(manifest_directory=str(manifests))
    manager._on_localization_mode(LocalizationMode.LOCALIZATION)
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
        assert system.backend.mapping_events == (['start', 'stop'] if owned_mapping else [])
        _wait_until(lambda: manager.downstream_execution_count == 0)
    finally:
        manager.begin_shutdown()
        _wait_until(lambda: manager.downstream_execution_count == 0)
        executor.shutdown(timeout_sec=TIMEOUT_S)
        thread.join(TIMEOUT_S)
        client.destroy()
        manager.destroy_node()
        rclpy.try_shutdown()
