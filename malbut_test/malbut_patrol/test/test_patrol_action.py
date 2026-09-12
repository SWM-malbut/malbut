"""Exercise patrol ownership and camera accounting through real ROS Actions."""

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
from sensor_msgs.msg import CameraInfo, Image
from tf2_ros import StaticTransformBroadcaster

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
                    handle.succeed()
                return NavigateToPose.Result()
            time.sleep(0.01)
        handle.abort()
        return NavigateToPose.Result()

    def _execute_spin(self, handle):
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
        self.emit_images = True
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
        self.costmap.publish(grid)
        info = CameraInfo()
        info.header.stamp = stamp
        info.header.frame_id = self.image_frame
        info.width = 2
        info.height = 2
        info.k = [2.0, 0.0, 1.0, 0.0, 2.0, 1.0, 0.0, 0.0, 1.0]
        self.info.publish(info)
        if self.emit_images:
            image = Image()
            image.header = info.header
            image.width = 2
            image.height = 2
            image.encoding = 'rgb8'
            image.step = 6
            image.data = [0] * 12
            self.image.publish(image)


@pytest.fixture
def patrol_runtime(request):
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
    }
    values.update(getattr(request, 'param', {}))
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
    assert 'partial coverage' in outcome.result.message


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


def test_costmap_patch_updates_candidate_safety_without_corrupting_grid():
    """Apply valid occupancy patches and ignore malformed extents atomically."""
    manager = object.__new__(PatrolManager)
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
