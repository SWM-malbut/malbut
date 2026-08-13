"""Asynchronous coordinate navigation delegated to standard Nav2 actions."""

from dataclasses import dataclass
from enum import Enum
from typing import Callable

from action_msgs.msg import GoalStatus
from geometry_msgs.msg import PoseStamped
from nav2_msgs.action import ComputePathToPose, NavigateToPose
from rclpy.action import ActionClient

from malbut_roaming.geometry import yaw_to_quaternion


class NavigationOutcome(Enum):
    """Terminal result of a validated Nav2 coordinate request."""

    SUCCEEDED = 'succeeded'
    FAILED = 'failed'
    CANCELED = 'canceled'


@dataclass(frozen=True)
class NavigationRequest:
    """One coordinate request and its owner."""

    token: int
    pose: PoseStamped
    source: str


ResultCallback = Callable[[NavigationRequest, NavigationOutcome, str], None]
StartCallback = Callable[[NavigationRequest], None]


def make_pose(node, frame_id: str, x: float, y: float, yaw: float) -> PoseStamped:
    """Create a timestamped planar Nav2 goal pose."""
    pose = PoseStamped()
    pose.header.frame_id = frame_id
    pose.header.stamp = node.get_clock().now().to_msg()
    pose.pose.position.x = float(x)
    pose.pose.position.y = float(y)
    quaternion = yaw_to_quaternion(yaw)
    pose.pose.orientation.x = quaternion[0]
    pose.pose.orientation.y = quaternion[1]
    pose.pose.orientation.z = quaternion[2]
    pose.pose.orientation.w = quaternion[3]
    return pose


class NavigationClient:
    """Plan, validate, and execute map coordinates through Nav2."""

    def __init__(
        self,
        node,
        planner_action: str,
        navigation_action: str,
        on_start: StartCallback,
        on_result: ResultCallback,
    ) -> None:
        """Attach Nav2 action clients to an existing ROS node."""
        self._node = node
        self._planner = ActionClient(
            node,
            ComputePathToPose,
            planner_action,
        )
        self._navigator = ActionClient(
            node,
            NavigateToPose,
            navigation_action,
        )
        self._on_start = on_start
        self._on_result = on_result
        self._token = 0
        self._request = None
        self._planner_handle = None
        self._navigation_handle = None
        self.feedback = None

    @property
    def active_request(self) -> NavigationRequest | None:
        """Return the current request, if any."""
        return self._request

    def is_ready(self) -> bool:
        """Return whether both required Nav2 actions are available."""
        return (
            self._planner.server_is_ready()
            and self._navigator.server_is_ready()
        )

    def request(self, pose: PoseStamped, source: str) -> NavigationRequest | None:
        """Preempt the old request and plan a new coordinate goal."""
        if not self.is_ready():
            return None
        self.cancel()
        self._token += 1
        request = NavigationRequest(self._token, pose, source)
        self._request = request
        goal = ComputePathToPose.Goal()
        goal.goal = pose
        goal.use_start = False
        future = self._planner.send_goal_async(goal)
        future.add_done_callback(
            lambda completed: self._planner_response(completed, request)
        )
        return request

    def cancel(self) -> None:
        """Invalidate and asynchronously cancel any current Nav2 work."""
        self._token += 1
        self._request = None
        self.feedback = None
        if self._planner_handle is not None:
            self._planner_handle.cancel_goal_async()
            self._planner_handle = None
        if self._navigation_handle is not None:
            self._navigation_handle.cancel_goal_async()
            self._navigation_handle = None

    def destroy(self) -> None:
        """Destroy action clients before their parent node."""
        self._planner.destroy()
        self._navigator.destroy()

    def _is_current(self, request: NavigationRequest) -> bool:
        return self._request is request and request.token == self._token

    def _planner_response(self, future, request: NavigationRequest) -> None:
        try:
            goal_handle = future.result()
        except Exception as error:  # noqa: B902 - rclpy future boundary
            self._finish(request, NavigationOutcome.FAILED, str(error))
            return
        if not goal_handle.accepted:
            self._finish(
                request,
                NavigationOutcome.FAILED,
                'Nav2 planner rejected the goal',
            )
            return
        if not self._is_current(request):
            goal_handle.cancel_goal_async()
            return
        self._planner_handle = goal_handle
        result_future = goal_handle.get_result_async()
        result_future.add_done_callback(
            lambda completed: self._planner_result(completed, request)
        )

    def _planner_result(self, future, request: NavigationRequest) -> None:
        if not self._is_current(request):
            return
        self._planner_handle = None
        try:
            wrapped = future.result()
        except Exception as error:  # noqa: B902 - rclpy future boundary
            self._finish(request, NavigationOutcome.FAILED, str(error))
            return
        if wrapped.status != GoalStatus.STATUS_SUCCEEDED:
            self._finish(
                request,
                NavigationOutcome.FAILED,
                f'Nav2 could not plan a path (status {wrapped.status})',
            )
            return
        if len(wrapped.result.path.poses) < 2:
            self._finish(
                request,
                NavigationOutcome.FAILED,
                'Nav2 returned an empty path',
            )
            return

        goal = NavigateToPose.Goal()
        goal.pose = request.pose
        future = self._navigator.send_goal_async(
            goal,
            feedback_callback=(
                lambda message: self._navigation_feedback(message, request)
            ),
        )
        future.add_done_callback(
            lambda completed: self._navigation_response(completed, request)
        )

    def _navigation_response(self, future, request: NavigationRequest) -> None:
        try:
            goal_handle = future.result()
        except Exception as error:  # noqa: B902 - rclpy future boundary
            self._finish(request, NavigationOutcome.FAILED, str(error))
            return
        if not goal_handle.accepted:
            self._finish(
                request,
                NavigationOutcome.FAILED,
                'Nav2 rejected the validated path goal',
            )
            return
        if not self._is_current(request):
            goal_handle.cancel_goal_async()
            return
        self._navigation_handle = goal_handle
        self._on_start(request)
        result_future = goal_handle.get_result_async()
        result_future.add_done_callback(
            lambda completed: self._navigation_result(completed, request)
        )

    def _navigation_feedback(self, message, request: NavigationRequest) -> None:
        if self._is_current(request):
            self.feedback = message.feedback

    def _navigation_result(self, future, request: NavigationRequest) -> None:
        if not self._is_current(request):
            return
        self._navigation_handle = None
        try:
            wrapped = future.result()
        except Exception as error:  # noqa: B902 - rclpy future boundary
            self._finish(request, NavigationOutcome.FAILED, str(error))
            return
        outcomes = {
            GoalStatus.STATUS_SUCCEEDED: NavigationOutcome.SUCCEEDED,
            GoalStatus.STATUS_CANCELED: NavigationOutcome.CANCELED,
        }
        outcome = outcomes.get(wrapped.status, NavigationOutcome.FAILED)
        self._finish(
            request,
            outcome,
            f'Nav2 navigation finished with status {wrapped.status}',
        )

    def _finish(
        self,
        request: NavigationRequest,
        outcome: NavigationOutcome,
        detail: str,
    ) -> None:
        if not self._is_current(request):
            return
        self._request = None
        self.feedback = None
        self._planner_handle = None
        self._navigation_handle = None
        self._on_result(request, outcome, detail)
