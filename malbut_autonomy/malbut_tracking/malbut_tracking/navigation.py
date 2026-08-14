"""Small asynchronous adapter around standard Nav2 motion actions."""

from enum import Enum
from typing import Callable

from action_msgs.msg import GoalStatus
from geometry_msgs.msg import PoseStamped
from nav2_msgs.action import ComputePathToPose, FollowPath, Spin
from nav_msgs.msg import Path
from rclpy.action import ActionClient


class MotionMode(Enum):
    """Kind of Nav2 motion currently owned by the follower."""

    NAVIGATE = 'navigate'
    SPIN = 'spin'


MotionResultCallback = Callable[[MotionMode, int, str], None]
PathResultCallback = Callable[[Path | None, str], None]


class Nav2PathClient:
    """Latest-only asynchronous client for Nav2 global path planning."""

    def __init__(self, node, action_name: str) -> None:
        self._client = ActionClient(node, ComputePathToPose, action_name)
        self._token = 0
        self._goal_handle = None

    def compute(
        self,
        goal_pose: PoseStamped,
        planner_id: str,
        callback: PathResultCallback,
    ) -> bool:
        """Plan from the current robot pose to one safe target pose."""
        if not self._client.server_is_ready():
            return False
        self.cancel()
        self._token += 1
        token = self._token
        goal = ComputePathToPose.Goal()
        goal.goal = goal_pose
        goal.planner_id = planner_id
        goal.use_start = False
        future = self._client.send_goal_async(goal)
        future.add_done_callback(
            lambda completed: self._goal_response(
                completed, token, callback
            )
        )
        return True

    def cancel(self) -> None:
        """Invalidate and cancel the currently relevant planning request."""
        self._token += 1
        if self._goal_handle is not None:
            self._goal_handle.cancel_goal_async()
            self._goal_handle = None

    def destroy(self) -> None:
        self.cancel()
        self._client.destroy()

    def _goal_response(
        self,
        future,
        token: int,
        callback: PathResultCallback,
    ) -> None:
        try:
            goal_handle = future.result()
        except Exception as error:  # noqa: B902 - rclpy future boundary
            if token == self._token:
                callback(None, f'Nav2 path request failed: {error}')
            return
        if token != self._token:
            if goal_handle.accepted:
                goal_handle.cancel_goal_async()
            return
        if not goal_handle.accepted:
            callback(None, 'Nav2 rejected ComputePathToPose')
            return
        self._goal_handle = goal_handle
        result_future = goal_handle.get_result_async()
        result_future.add_done_callback(
            lambda completed: self._result(
                completed, token, callback
            )
        )

    def _result(
        self,
        future,
        token: int,
        callback: PathResultCallback,
    ) -> None:
        if token != self._token:
            return
        self._goal_handle = None
        try:
            wrapped = future.result()
            if wrapped.status != GoalStatus.STATUS_SUCCEEDED:
                callback(
                    None,
                    f'Nav2 path planning finished with status '
                    f'{wrapped.status}',
                )
                return
            callback(wrapped.result.path, 'Nav2 path planning succeeded')
        except Exception as error:  # noqa: B902 - rclpy future boundary
            callback(None, f'Nav2 path result failed: {error}')


class Nav2MotionClient:
    """Preemptible Nav2 motion clients with stale-result guards."""

    def __init__(
        self,
        node,
        follow_path_action: str,
        spin_action: str,
        on_result: MotionResultCallback,
    ) -> None:
        """Attach standard Nav2 action clients to a ROS node."""
        self._follow_path_client = ActionClient(
            node,
            FollowPath,
            follow_path_action,
        )
        self._spin_client = ActionClient(node, Spin, spin_action)
        self._on_result = on_result
        self._token = 0
        self._mode: MotionMode | None = None
        self._pending = False
        self._goal_handle = None

    @property
    def mode(self) -> MotionMode | None:
        """Return the current requested motion mode."""
        return self._mode

    def follow_path(
        self,
        path: Path,
        controller_id: str,
        goal_checker_id: str,
    ) -> bool:
        """Preempt current work with an already planned path."""
        if not self._follow_path_client.server_is_ready():
            return False
        goal = FollowPath.Goal()
        goal.path = path
        goal.controller_id = controller_id
        goal.goal_checker_id = goal_checker_id
        return self._send(
            self._follow_path_client,
            goal,
            MotionMode.NAVIGATE,
        )

    def spin(
        self,
        target_yaw: float,
        allowance_seconds: float,
    ) -> bool:
        """Preempt current work and request a relative Nav2 body rotation."""
        if not self._spin_client.server_is_ready():
            return False
        goal = Spin.Goal()
        goal.target_yaw = float(target_yaw)
        seconds = max(0.0, float(allowance_seconds))
        goal.time_allowance.sec = int(seconds)
        goal.time_allowance.nanosec = int((seconds % 1.0) * 1e9)
        return self._send(self._spin_client, goal, MotionMode.SPIN)

    def cancel(self) -> None:
        """Invalidate and asynchronously cancel follower-owned Nav2 work."""
        self._token += 1
        self._pending = False
        self._mode = None
        if self._goal_handle is not None:
            self._goal_handle.cancel_goal_async()
            self._goal_handle = None

    def destroy(self) -> None:
        """Release action clients before their parent node is destroyed."""
        self.cancel()
        self._follow_path_client.destroy()
        self._spin_client.destroy()

    def _send(self, client, goal, mode: MotionMode) -> bool:
        replacing_navigation = (
            mode == MotionMode.NAVIGATE
            and self._mode == MotionMode.NAVIGATE
        )
        # Controller Server accepts a replacement FollowPath goal. Sending it
        # directly preserves continuous motion without cancel/stop gaps.
        if not replacing_navigation:
            self.cancel()
        self._token += 1
        token = self._token
        self._mode = mode
        self._pending = True
        future = client.send_goal_async(goal)
        future.add_done_callback(
            lambda completed: self._goal_response(completed, token, mode)
        )
        return True

    def _goal_response(self, future, token: int, mode: MotionMode) -> None:
        try:
            goal_handle = future.result()
        except Exception as error:  # noqa: B902 - rclpy future boundary
            self._finish(token, mode, GoalStatus.STATUS_ABORTED, str(error))
            return
        if token != self._token or self._mode != mode:
            if goal_handle.accepted:
                goal_handle.cancel_goal_async()
            return
        self._pending = False
        if not goal_handle.accepted:
            self._finish(
                token,
                mode,
                GoalStatus.STATUS_ABORTED,
                f'Nav2 rejected {mode.value}',
            )
            return
        self._goal_handle = goal_handle
        result_future = goal_handle.get_result_async()
        result_future.add_done_callback(
            lambda completed: self._result(completed, token, mode)
        )

    def _result(self, future, token: int, mode: MotionMode) -> None:
        try:
            wrapped = future.result()
            status = wrapped.status
            detail = f'Nav2 {mode.value} finished with status {status}'
        except Exception as error:  # noqa: B902 - rclpy future boundary
            status = GoalStatus.STATUS_ABORTED
            detail = str(error)
        self._finish(token, mode, status, detail)

    def _finish(
        self,
        token: int,
        mode: MotionMode,
        status: int,
        detail: str,
    ) -> None:
        if token != self._token or self._mode != mode:
            return
        self._pending = False
        self._goal_handle = None
        self._mode = None
        self._on_result(mode, status, detail)
