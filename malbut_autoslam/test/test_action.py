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
from geometry_msgs.msg import TransformStamped
from malbut_interfaces.action import AutoSlam
from nav2_msgs.action import NavigateToPose
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

    def __init__(self, context, prefix, directory, scene, navigation):
        super().__init__('autoslam_test_backend', context=context)
        self.directory = directory
        self.scene = scene
        self.started = Event()
        self.cancel_seen = Event()
        self.release_cancel = Event()
        self.stopping = Event()
        self.save_requests = []
        self.group = ReentrantCallbackGroup()
        self.publisher = self.create_publisher(
            OccupancyGrid, prefix + '/map',
            QoSProfile(depth=1, durability=DurabilityPolicy.TRANSIENT_LOCAL))
        self.broadcaster = TransformBroadcaster(self)
        self.create_timer(0.05, self._publish)
        self.create_service(
            SaveMap, prefix + '/save_map', self._save, callback_group=self.group)
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

    def _navigate(self, handle):
        self.started.set()
        while not handle.is_cancel_requested and not self.stopping.wait(0.01):
            pass
        if handle.is_cancel_requested:
            self.cancel_seen.set()
            if self.release_cancel.wait(TIMEOUT_S):
                handle.canceled()
            else:
                handle.abort()
        else:
            handle.abort()
        return NavigateToPose.Result()

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

    def __init__(self, directory, scene='complete', navigation=True, **settings):
        self.context = Context()
        rclpy.init(context=self.context, domain_id=160 + os.getpid() % 30)
        self.prefix = '/test_autoslam_' + uuid4().hex
        self.backend = _Backend(
            self.context, self.prefix, directory, scene, navigation)
        defaults = {
            'map_directory': str(directory), 'map_topic': self.prefix + '/map',
            'navigation_action': self.prefix + '/navigate',
            'save_map_service': self.prefix + '/save_map',
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


def test_missing_navigation_backend_returns_failure(system_factory):
    """A live map does not make the operation ready without Nav2."""
    system = system_factory(navigation=False, ready_timeout_s=0.15)
    handle = system.request()
    result = _result(handle.get_result_async())
    assert result.status == GoalStatus.STATUS_ABORTED
    assert not result.result.success
    assert 'prerequisites not ready' in result.result.message
    assert not system.backend.save_requests


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
