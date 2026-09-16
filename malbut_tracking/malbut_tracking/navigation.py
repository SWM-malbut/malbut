"""Small asynchronous adapter around standard Nav2 motion actions."""

from dataclasses import dataclass
from enum import Enum
import math
import time
from typing import Callable

from action_msgs.msg import GoalStatus
from geometry_msgs.msg import PoseStamped
from nav2_msgs.action import ComputePathToPose, FollowPath, Spin
from nav_msgs.msg import Path
from rclpy.action import ActionClient
from rclpy.clock import Clock, ClockType


class MotionMode(Enum):
    """Kind of Nav2 motion currently owned by the follower."""

    NAVIGATE = 'navigate'
    SPIN = 'spin'


MotionResultCallback = Callable[[MotionMode, int, str], None]
MotionFeedbackCallback = Callable[[MotionMode, object], None]
PathResultCallback = Callable[[Path | None, str], None]
PATH_TIMEOUT_PREFIX = 'Nav2 path planning timed out'


class Nav2PathClient:
    """Bound the response wait without overlapping uncanceled Nav2 planning."""

    def __init__(
        self, node, action_name: str, on_idle: Callable[[], None] | None = None,
    ) -> None:
        self._node = node
        self._client = ActionClient(node, ComputePathToPose, action_name)
        self._on_idle = on_idle
        self._timer_clock = Clock(clock_type=ClockType.STEADY_TIME)
        self._token = 0
        self._pending = False
        self._goal_handle = None
        self._callback: PathResultCallback | None = None
        self._cancel_requested = False
        self._cancel_sent = False
        self._deadline = None
        self._deadline_timer = None
        self._destroyed = False

    @property
    def busy(self) -> bool:
        """Return whether one path request is awaiting a result."""
        return self._pending or self._goal_handle is not None

    def compute(
        self,
        goal_pose: PoseStamped,
        planner_id: str,
        callback: PathResultCallback,
        *,
        timeout_seconds: float | None = None,
    ) -> bool:
        """Plan once; a deadline limits waiting, not the server's CPU work."""
        if timeout_seconds is not None and (
            not math.isfinite(timeout_seconds) or timeout_seconds <= 0.0
        ):
            raise ValueError('planning timeout must be finite and positive')
        if self._destroyed or not self._client.server_is_ready() or self.busy:
            return False
        self._token += 1
        token = self._token
        goal = ComputePathToPose.Goal()
        goal.goal = goal_pose
        goal.planner_id = planner_id
        goal.use_start = False
        self._pending = True
        self._callback = callback
        self._cancel_requested = False
        self._cancel_sent = False
        if timeout_seconds is not None:
            self._deadline = time.monotonic() + timeout_seconds
            self._deadline_timer = self._node.create_timer(
                timeout_seconds, lambda: self._expire_if_due(token),
                clock=self._timer_clock,
            )
        try:
            future = self._client.send_goal_async(goal)
        except Exception as error:  # noqa: B902 - rclpy transport boundary
            self._unconfirmed(f'Nav2 path request failed: {error}')
            return True
        future.add_done_callback(
            lambda completed: self._goal_response(completed, token)
        )
        return True

    def cancel(self) -> None:
        """Invalidate its result but retain ownership until Nav2 finishes."""
        self._callback = None
        self._clear_deadline()
        self._cancel_requested = self.busy
        self._send_cancel()

    def destroy(self) -> None:
        self._destroyed = True
        self.cancel()
        self._client.destroy()

    def _clear_deadline(self) -> None:
        self._deadline = None
        timer, self._deadline_timer = self._deadline_timer, None
        if timer is not None:
            timer.cancel()
            self._node.destroy_timer(timer)

    def _send_cancel(self) -> None:
        if not self._cancel_requested or self._goal_handle is None or self._cancel_sent:
            return
        self._cancel_sent = True
        try:
            self._goal_handle.cancel_goal_async()
        except Exception:  # noqa: B902 - failure does not release ownership
            self._cancel_sent = False

    def _expire_if_due(self, token: int) -> None:
        if (self._destroyed or token != self._token or self._deadline is None
                or time.monotonic() < self._deadline):
            return
        callback, self._callback = self._callback, None
        self._clear_deadline()
        self._cancel_requested = True
        self._send_cancel()
        if callback is not None:
            callback(None, f'{PATH_TIMEOUT_PREFIX}; waiting for Nav2 to finish cancellation')

    def _goal_response(self, future, token: int) -> None:
        if token == self._token:
            self._expire_if_due(token)
        try:
            goal_handle = future.result()
        except Exception as error:  # noqa: B902 - rclpy future boundary
            if token == self._token:
                self._unconfirmed(f'Nav2 path request failed: {error}')
            return
        if token != self._token or self._destroyed:
            if goal_handle.accepted:
                try:
                    goal_handle.cancel_goal_async()
                except Exception:  # noqa: B902 - shutdown transport boundary
                    pass
            return
        if not goal_handle.accepted:
            self._finish(None, 'Nav2 rejected ComputePathToPose')
            return
        self._pending = False
        self._goal_handle = goal_handle
        self._send_cancel()
        try:
            result_future = goal_handle.get_result_async()
        except Exception as error:  # noqa: B902 - rclpy transport boundary
            self._unconfirmed(f'Nav2 path result failed: {error}')
            return
        result_future.add_done_callback(
            lambda completed: self._result(completed, token)
        )

    def _result(self, future, token: int) -> None:
        if token != self._token or self._destroyed:
            return
        # A result callback may run before an already-due timer callback.
        # Check wall time here as well so an overdue path cannot slip through.
        self._expire_if_due(token)
        try:
            wrapped = future.result()
            if wrapped.status not in {
                GoalStatus.STATUS_SUCCEEDED,
                GoalStatus.STATUS_CANCELED,
                GoalStatus.STATUS_ABORTED,
            }:
                raise RuntimeError(f'nonterminal status {wrapped.status}')
            path = (
                wrapped.result.path
                if wrapped.status == GoalStatus.STATUS_SUCCEEDED else None
            )
        except Exception as error:  # noqa: B902 - rclpy future boundary
            self._unconfirmed(f'Nav2 path result failed: {error}')
            return
        self._finish(
            path,
            'Nav2 path planning succeeded' if path is not None else
            f'Nav2 path planning finished with status {wrapped.status}',
        )

    def _unconfirmed(self, detail: str) -> None:
        """Retain ownership when a missing response cannot prove completion."""
        callback, self._callback = self._callback, None
        self._clear_deadline()
        self._cancel_requested = True
        self._send_cancel()
        if callback is not None and not self._destroyed:
            callback(None, f'{detail}; completion is unconfirmed')

    def _finish(self, path: Path | None, detail: str) -> None:
        callback, self._callback = self._callback, None
        self._clear_deadline()
        self._pending = False
        self._goal_handle = None
        self._cancel_requested = False
        self._cancel_sent = False
        if self._destroyed:
            return
        if callback is not None:
            callback(path, detail)
        elif self._on_idle is not None:
            self._on_idle()


@dataclass
class _MotionRequest:
    """Keep an owned goal until its acceptance and terminal are both known."""

    client: object
    goal: object
    mode: MotionMode
    token: int
    handle: object = None
    cancel_requested: bool = False


class Nav2MotionClient:
    """Replace paths continuously and switch other motion only after stopping."""

    def __init__(
        self,
        node,
        follow_path_action: str,
        spin_action: str,
        on_result: MotionResultCallback,
        on_feedback: MotionFeedbackCallback | None = None,
        on_idle: Callable[[], None] | None = None,
    ) -> None:
        """Attach standard Nav2 action clients to a ROS node."""
        self._follow_path_client = ActionClient(
            node,
            FollowPath,
            follow_path_action,
        )
        self._spin_client = ActionClient(node, Spin, spin_action)
        self._on_result = on_result
        self._on_feedback = on_feedback
        self._on_idle = on_idle
        self._token = 0
        self._mode: MotionMode | None = None
        self._requests: dict[int, _MotionRequest] = {}
        self._queued: _MotionRequest | None = None
        self._stopping = False
        self._destroyed = False

    @property
    def mode(self) -> MotionMode | None:
        """Return the current requested motion mode."""
        return self._mode

    @property
    def busy(self) -> bool:
        """Return whether motion is awaiting acceptance, completion, or stop."""
        return bool(self._requests) or self._queued is not None

    @property
    def stopping(self) -> bool:
        """Return whether replacement is waiting for previous motion to end."""
        return self._stopping

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
        """Discard queued motion and retain owned goals until they finish."""
        self._token += 1
        self._mode = None
        self._queued = None
        self._stop_requests()

    def destroy(self) -> None:
        """Release action clients before their parent node is destroyed."""
        self.cancel()
        self._destroyed = True
        self._follow_path_client.destroy()
        self._spin_client.destroy()

    def _send(self, client, goal, mode: MotionMode) -> bool:
        if self._destroyed:
            return False
        replacing_navigation = (
            mode == MotionMode.NAVIGATE
            and self._mode == MotionMode.NAVIGATE
            and not self._stopping
            and all(
                request.mode == MotionMode.NAVIGATE
                for request in self._requests.values()
            )
        )
        self._token += 1
        request = _MotionRequest(client, goal, mode, self._token)
        self._mode = mode
        # Controller Server accepts a replacement FollowPath goal. Sending it
        # directly preserves continuous motion without cancel/stop gaps.
        # Humble's Spin does not support preemption. Its terminal result, not
        # the cancel acknowledgement, is the boundary for another motion.
        if self._requests and not (
            replacing_navigation and self._can_replace_path()
        ):
            self._queued = request
            if not replacing_navigation:
                self._stop_requests()
        else:
            self._dispatch(request)
        return True

    def _can_replace_path(self) -> bool:
        return (
            len(self._requests) == 1
            and next(iter(self._requests.values())).handle is not None
        )

    def _dispatch_queued(self) -> None:
        queued = self._queued
        if queued is None:
            return
        if self._requests and not (
            not self._stopping
            and queued.mode == MotionMode.NAVIGATE
            and self._can_replace_path()
            and all(
                request.mode == MotionMode.NAVIGATE
                for request in self._requests.values()
            )
        ):
            return
        self._queued = None
        self._dispatch(queued)

    def _stop_requests(self) -> None:
        self._stopping = bool(self._requests)
        for request in tuple(self._requests.values()):
            self._request_cancel(request)

    @staticmethod
    def _request_cancel(request: _MotionRequest) -> None:
        if request.handle is None or request.cancel_requested:
            return
        request.cancel_requested = True
        try:
            request.handle.cancel_goal_async()
        except Exception:  # noqa: B902 - a transport failure is not a stop
            request.cancel_requested = False

    def _dispatch(self, request: _MotionRequest) -> None:
        token, mode = request.token, request.mode
        self._requests[token] = request
        feedback_callback = None
        if self._on_feedback is not None:
            def feedback_callback(message):
                self._feedback(message, token, mode)
        try:
            future = request.client.send_goal_async(
                request.goal,
                feedback_callback=feedback_callback,
            )
        except Exception as error:  # noqa: B902 - rclpy transport boundary
            self._uncertain(token, mode, error)
            return
        future.add_done_callback(
            lambda completed: self._goal_response(completed, token, mode)
        )

    def _feedback(self, message, token: int, mode: MotionMode) -> None:
        """Forward feedback only for the currently relevant Nav2 goal."""
        if (
            token != self._token
            or self._mode != mode
            or self._on_feedback is None
        ):
            return
        self._on_feedback(mode, message.feedback)

    def _goal_response(self, future, token: int, mode: MotionMode) -> None:
        try:
            goal_handle = future.result()
        except Exception as error:  # noqa: B902 - rclpy future boundary
            self._uncertain(token, mode, error)
            return
        request = self._requests.get(token)
        if request is None or self._destroyed:
            if goal_handle.accepted:
                self._request_cancel(
                    _MotionRequest(None, None, mode, token, goal_handle)
                )
            return
        if not goal_handle.accepted:
            self._finish(
                token,
                mode,
                GoalStatus.STATUS_ABORTED,
                f'Nav2 rejected {mode.value}',
            )
            return
        request.handle = goal_handle
        if self._stopping or any(other > token for other in self._requests):
            self._request_cancel(request)
        try:
            result_future = goal_handle.get_result_async()
        except Exception as error:  # noqa: B902 - rclpy transport boundary
            self._uncertain(token, mode, error)
            return
        result_future.add_done_callback(
            lambda completed: self._result(completed, token, mode)
        )
        self._dispatch_queued()

    def _result(self, future, token: int, mode: MotionMode) -> None:
        try:
            wrapped = future.result()
            status = wrapped.status
            if status not in {
                GoalStatus.STATUS_SUCCEEDED,
                GoalStatus.STATUS_CANCELED,
                GoalStatus.STATUS_ABORTED,
            }:
                raise RuntimeError(f'Nav2 returned nonterminal status {status}')
            detail = f'Nav2 {mode.value} finished with status {status}'
        except Exception as error:  # noqa: B902 - rclpy future boundary
            self._uncertain(token, mode, error)
            return
        self._finish(token, mode, status, detail)

    def _uncertain(self, token: int, mode: MotionMode, error: Exception) -> None:
        """Keep ownership when a transport error cannot prove motion stopped."""
        if token not in self._requests or self._destroyed:
            return
        self._queued = None
        self._mode = None
        self._stop_requests()
        self._on_result(
            mode,
            GoalStatus.STATUS_ABORTED,
            f'Nav2 {mode.value} stop is unconfirmed: {error}',
        )

    def _finish(
        self,
        token: int,
        mode: MotionMode,
        status: int,
        detail: str,
    ) -> None:
        if self._requests.pop(token, None) is None or self._destroyed:
            return
        notify = token == self._token and self._mode == mode
        if notify:
            self._mode = None
            # If a replacement was rejected, its older FollowPath may still
            # be running. Retain/cancel it before accepting another mode.
            if self._requests:
                self._stop_requests()
        if not self._requests:
            self._stopping = False
        became_idle = not self.busy
        self._dispatch_queued()
        if notify:
            self._on_result(mode, status, detail)
        if (became_idle and not self.busy and not self._destroyed
                and self._on_idle is not None):
            self._on_idle()
