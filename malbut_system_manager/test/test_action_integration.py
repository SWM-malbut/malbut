"""ROS integration test for unified execution, preemption, and resume."""

from threading import Event, Thread
import time

from action_msgs.msg import GoalStatus
import pytest
import rclpy
from rclpy.action import (
    ActionClient,
    ActionServer,
    CancelResponse,
    GoalResponse,
)
from rclpy.callback_groups import ReentrantCallbackGroup
from rclpy.executors import MultiThreadedExecutor
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy

from malbut_interfaces.action import ExecuteMission, FollowPerson
from malbut_interfaces.msg import SystemState
from nav2_msgs.action import NavigateToPose
from malbut_system_manager.system_manager_node import (
    SystemManagerNode,
    _require_positive_finite,
)


TIMEOUT_S = 8.0


@pytest.mark.parametrize('value', [0.0, -1.0, float('inf'), float('nan')])
def test_runtime_timeouts_must_be_positive_and_finite(value):
    """Reject watchdog values that rclpy cannot schedule safely."""
    with pytest.raises(ValueError, match='finite number greater than zero'):
        _require_positive_finite('test_timeout_s', value)


class _FollowPersonServer(Node):
    """Controllable downstream server that records every execution."""

    def __init__(self, *, cancel_delay_s=0.0) -> None:
        super().__init__('test_follow_person_server')
        self.requests = []
        self.started = Event()
        self._cancel_delay_s = cancel_delay_s
        self._server = ActionServer(
            self,
            FollowPerson,
            '/follow_person',
            execute_callback=self._execute,
            goal_callback=lambda _request: GoalResponse.ACCEPT,
            cancel_callback=self._cancel_request,
            callback_group=ReentrantCallbackGroup(),
        )

    def destroy_node(self) -> bool:
        self._server.destroy()
        return super().destroy_node()

    def _execute(self, goal_handle):
        request = goal_handle.request
        self.requests.append(
            (
                request.target_mode,
                request.target_person_id,
                request.desired_distance_m,
            )
        )
        self.started.set()
        feedback = FollowPerson.Feedback()
        feedback.state = 'TRACKING'
        feedback.target_visible = True
        goal_handle.publish_feedback(feedback)

        if request.target_person_id != 'first':
            result = FollowPerson.Result()
            result.success = True
            result.final_state = 'STOPPED'
            result.message = 'completed by fake server'
            goal_handle.succeed()
            return result

        deadline = time.monotonic() + TIMEOUT_S
        while not goal_handle.is_cancel_requested:
            if time.monotonic() >= deadline:
                result = FollowPerson.Result()
                result.success = False
                result.final_state = 'TARGET_LOST'
                result.message = 'test timeout'
                goal_handle.abort()
                return result
            time.sleep(0.01)
        result = FollowPerson.Result()
        result.success = False
        result.final_state = 'STOPPED'
        result.message = 'canceled by manager'
        goal_handle.canceled()
        return result

    def _cancel_request(self, _goal_handle):
        if self._cancel_delay_s:
            time.sleep(self._cancel_delay_s)
        return CancelResponse.ACCEPT


class _DelayedCancelManager(SystemManagerNode):
    """Hold the cancel callback open to expose public state races."""

    def _cancel(self, goal_handle):
        response = super()._cancel(goal_handle)
        time.sleep(0.2)
        return response


class _NavigateToPoseServer(Node):
    """Successful fake Nav2 server used to verify dynamic dispatch."""

    def __init__(self) -> None:
        super().__init__('test_navigate_to_pose_server')
        self.requests = []
        self._server = ActionServer(
            self,
            NavigateToPose,
            '/navigate_to_pose',
            execute_callback=self._execute,
            goal_callback=lambda _request: GoalResponse.ACCEPT,
            cancel_callback=lambda _handle: CancelResponse.ACCEPT,
            callback_group=ReentrantCallbackGroup(),
        )

    def destroy_node(self) -> bool:
        self._server.destroy()
        return super().destroy_node()

    def _execute(self, goal_handle):
        self.requests.append(goal_handle.request)
        result = NavigateToPose.Result()
        goal_handle.succeed()
        return result


def _wait_until(predicate, timeout=TIMEOUT_S):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError('condition did not become true before timeout')


def _wait_future(future, timeout=TIMEOUT_S):
    _wait_until(future.done, timeout)
    return future.result()


def test_execute_preempt_resume_cancel_and_transient_state():
    """One public goal remains alive while its downstream goal is resumed."""
    rclpy.init()
    downstream = _FollowPersonServer()
    navigation = _NavigateToPoseServer()
    manager = SystemManagerNode()
    client_node = Node('test_system_manager_client')
    executor = MultiThreadedExecutor(num_threads=6)
    for node in (downstream, navigation, manager, client_node):
        executor.add_node(node)
    spin_thread = Thread(target=executor.spin, daemon=True)
    spin_thread.start()
    client = ActionClient(
        client_node,
        ExecuteMission,
        '/malbut/mission/execute',
    )

    try:
        assert client.wait_for_server(timeout_sec=TIMEOUT_S)

        default_feedback = []
        default_goal = ExecuteMission.Goal()
        default_goal.capability_id = 'follow_person'
        default_goal.arguments_yaml = '{}'
        default_handle = _wait_future(
            client.send_goal_async(
                default_goal,
                feedback_callback=lambda message: default_feedback.append(
                    message.feedback.state
                ),
            )
        )
        default_result = _wait_future(default_handle.get_result_async())
        assert default_result.status == GoalStatus.STATUS_SUCCEEDED
        assert downstream.requests[0] == (0, '', pytest.approx(1.0))
        assert 'success: true' in default_result.result.result_yaml
        assert 'RUNNING' in default_feedback

        first_feedback = []
        first_goal = ExecuteMission.Goal()
        first_goal.capability_id = 'follow_person'
        first_goal.arguments_yaml = 'target_person_id: first\n'
        first_handle = _wait_future(
            client.send_goal_async(
                first_goal,
                feedback_callback=lambda message: first_feedback.append(
                    message.feedback.state
                ),
            )
        )
        assert first_handle.accepted
        _wait_until(lambda: len(downstream.requests) >= 2)

        navigate_goal = ExecuteMission.Goal()
        navigate_goal.capability_id = 'navigate_to_pose'
        navigate_goal.arguments_yaml = (
            'pose:\n'
            '  header:\n'
            '    frame_id: map\n'
            '  pose:\n'
            '    position:\n'
            '      x: 2.5\n'
            '      y: -1.25\n'
            '    orientation:\n'
            '      w: 1.0\n'
        )
        navigate_handle = _wait_future(
            client.send_goal_async(navigate_goal)
        )
        navigate_result = _wait_future(
            navigate_handle.get_result_async()
        )
        assert navigate_result.status == GoalStatus.STATUS_SUCCEEDED
        assert len(navigation.requests) == 1
        navigation_request = navigation.requests[0]
        assert navigation_request.pose.header.frame_id == 'map'
        assert navigation_request.pose.pose.position.x == pytest.approx(2.5)
        assert navigation_request.pose.pose.position.y == pytest.approx(-1.25)
        assert navigation_request.pose.pose.orientation.w == pytest.approx(1.0)
        assert navigation_request.behavior_tree == ''

        _wait_until(lambda: len(downstream.requests) >= 3)
        assert [item[1] for item in downstream.requests[1:3]] == [
            'first',
            'first',
        ]
        assert 'CANCELING' in first_feedback
        assert 'SUSPENDED' in first_feedback
        assert first_handle.get_result_async().done() is False

        cancel_response = _wait_future(first_handle.cancel_goal_async())
        assert cancel_response.goals_canceling
        first_result = _wait_future(first_handle.get_result_async())
        assert first_result.status == GoalStatus.STATUS_CANCELED

        state_event = Event()
        states = []
        qos = QoSProfile(depth=1)
        qos.reliability = ReliabilityPolicy.RELIABLE
        qos.durability = DurabilityPolicy.TRANSIENT_LOCAL

        def receive_state(message):
            states.append(message)
            state_event.set()

        subscription = client_node.create_subscription(
            SystemState,
            '/malbut/state',
            receive_state,
            qos,
        )
        assert state_event.wait(TIMEOUT_S)
        assert states[-1].system_state == SystemState.IDLE
        client_node.destroy_subscription(subscription)
    finally:
        client.destroy()
        executor.shutdown(timeout_sec=TIMEOUT_S)
        spin_thread.join(timeout=TIMEOUT_S)
        for node in (client_node, manager, navigation, downstream):
            executor.remove_node(node)
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


def test_pending_cancel_waits_for_rclpy_cancel_transition():
    """An immediately completed pending cancel must remain CANCELED."""
    rclpy.init()
    downstream = _FollowPersonServer(cancel_delay_s=0.5)
    navigation = _NavigateToPoseServer()
    manager = _DelayedCancelManager()
    client_node = Node('test_pending_cancel_client')
    executor = MultiThreadedExecutor(num_threads=8)
    for node in (downstream, navigation, manager, client_node):
        executor.add_node(node)
    spin_thread = Thread(target=executor.spin, daemon=True)
    spin_thread.start()
    client = ActionClient(
        client_node,
        ExecuteMission,
        '/malbut/mission/execute',
    )

    try:
        assert client.wait_for_server(timeout_sec=TIMEOUT_S)
        blocker = ExecuteMission.Goal()
        blocker.capability_id = 'follow_person'
        blocker.arguments_yaml = 'target_person_id: first\n'
        blocker_handle = _wait_future(client.send_goal_async(blocker))
        assert blocker_handle.accepted
        assert downstream.started.wait(TIMEOUT_S)

        pending = ExecuteMission.Goal()
        pending.capability_id = 'navigate_to_pose'
        pending.arguments_yaml = (
            'pose:\n'
            '  header:\n'
            '    frame_id: map\n'
            '  pose:\n'
            '    orientation:\n'
            '      w: 1.0\n'
        )
        pending_handle = _wait_future(client.send_goal_async(pending))
        assert pending_handle.accepted
        _wait_until(lambda: len(manager._state.pending) == 1)

        cancel_response = _wait_future(
            pending_handle.cancel_goal_async()
        )
        assert cancel_response.goals_canceling
        pending_result = _wait_future(pending_handle.get_result_async())
        assert pending_result.status == GoalStatus.STATUS_CANCELED
    finally:
        manager.begin_shutdown()
        try:
            _wait_until(
                lambda: manager.downstream_execution_count == 0,
                timeout=TIMEOUT_S,
            )
        except AssertionError:
            manager.force_shutdown()
        client.destroy()
        executor.shutdown(timeout_sec=TIMEOUT_S)
        spin_thread.join(timeout=TIMEOUT_S)
        for node in (client_node, manager, navigation, downstream):
            executor.remove_node(node)
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()
