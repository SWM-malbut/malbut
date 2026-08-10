"""ROS adapter that sends scheduled patrol goals to Nav2."""

import json
from pathlib import Path
import time
from typing import Callable

from action_msgs.msg import GoalStatus
from geometry_msgs.msg import PoseStamped
from nav2_msgs.action import NavigateToPose
import rclpy
from rclpy.action import ActionClient
from rclpy.node import Node
from rclpy.qos import DurabilityPolicy, QoSProfile, ReliabilityPolicy
from std_msgs.msg import String
from std_srvs.srv import Trigger

from malbut_patrol.geometry import yaw_to_quaternion
from malbut_patrol.patrol_state import (
    PatrolCommand,
    PatrolProgress,
    PatrolState,
    Transition,
)
from malbut_patrol.route_loader import load_route


class PatrolManager(Node):
    """Orchestrate patrol timing while delegating motion entirely to Nav2."""

    def __init__(self) -> None:
        """Load the route and expose patrol services and state."""
        super().__init__('patrol_manager')
        self.declare_parameter('route_file', '')
        self.declare_parameter('autostart', False)
        self.declare_parameter('nav2_action_name', 'navigate_to_pose')
        self.declare_parameter('nav2_server_timeout_seconds', 30.0)

        route_file = str(self.get_parameter('route_file').value).strip()
        if not route_file:
            raise ValueError('route_file parameter must not be empty')
        self._route = load_route(Path(route_file))
        self._progress = PatrolProgress(self._route)
        self._action_name = str(
            self.get_parameter('nav2_action_name').value
        ).strip()
        if not self._action_name:
            raise ValueError('nav2_action_name must not be empty')
        self._server_timeout = float(
            self.get_parameter('nav2_server_timeout_seconds').value
        )
        if self._server_timeout <= 0.0:
            raise ValueError(
                'nav2_server_timeout_seconds must be greater than zero'
            )

        self._goal_token = 0
        self._pending_server = False
        self._server_deadline = 0.0
        self._request_pending = False
        self._active_goal = None
        self._cancelling_tokens: set[int] = set()
        self._deferred_send = False
        self._phase_deadline_ns: int | None = None
        self._phase_command = PatrolCommand.NONE
        self._paused_phase_remaining: float | None = None
        self._detail = 'patrol manager ready'

        status_qos = QoSProfile(
            depth=1,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
            reliability=ReliabilityPolicy.RELIABLE,
        )
        self._status_publisher = self.create_publisher(
            String,
            'patrol/status',
            status_qos,
        )
        self._action_client = ActionClient(
            self,
            NavigateToPose,
            self._action_name,
        )
        self.create_service(Trigger, 'patrol/start', self._start_callback)
        self.create_service(Trigger, 'patrol/pause', self._pause_callback)
        self.create_service(Trigger, 'patrol/resume', self._resume_callback)
        self.create_service(Trigger, 'patrol/stop', self._stop_callback)
        self._timer = self.create_timer(0.1, self._tick)
        self._publish_status()

        if bool(self.get_parameter('autostart').value):
            self._apply(self._progress.start())

    @property
    def shutdown_pending(self) -> bool:
        """Return whether an asynchronous request still needs to settle."""
        return self._request_pending or bool(self._cancelling_tokens)

    def request_shutdown(self) -> None:
        """Stop patrol and request cancellation before node shutdown."""
        if self._progress.state == PatrolState.STOPPING:
            return
        if self._progress.state != PatrolState.IDLE:
            self._apply(self._progress.stop())
        else:
            self._invalidate_goal()

    def _start_callback(self, _request, response):
        if self._request_pending or self._cancelling_tokens:
            return self._response(
                response,
                False,
                'wait for the previous Nav2 goal to finish cancelling',
            )
        try:
            transition = self._progress.start()
        except RuntimeError as error:
            return self._response(response, False, str(error))
        self._paused_phase_remaining = None
        self._apply(transition)
        return self._response(response, True, transition.message)

    def _pause_callback(self, _request, response):
        remaining = self._remaining_phase_seconds()
        try:
            transition = self._progress.pause()
        except RuntimeError as error:
            return self._response(response, False, str(error))
        self._paused_phase_remaining = (
            remaining
            if self._progress.state == PatrolState.PAUSED
            else None
        )
        self._apply(transition)
        return self._response(response, True, self._detail)

    def _resume_callback(self, _request, response):
        if self._request_pending or self._cancelling_tokens:
            return self._response(
                response,
                False,
                'wait for the Nav2 goal cancellation to finish',
            )
        try:
            transition = self._progress.resume()
        except RuntimeError as error:
            return self._response(response, False, str(error))
        if (
            self._paused_phase_remaining is not None
            and transition.command
            in {
                PatrolCommand.START_DWELL,
                PatrolCommand.START_INTERVAL_WAIT,
                PatrolCommand.START_RETRY_WAIT,
            }
        ):
            transition = Transition(
                transition.command,
                self._paused_phase_remaining,
                transition.message,
            )
        self._paused_phase_remaining = None
        self._apply(transition)
        return self._response(response, True, self._detail)

    def _stop_callback(self, _request, response):
        if self._progress.state == PatrolState.IDLE:
            return self._response(response, False, 'patrol is already stopped')
        try:
            transition = self._progress.stop()
        except RuntimeError as error:
            return self._response(response, False, str(error))
        self._paused_phase_remaining = None
        self._apply(transition)
        return self._response(response, True, self._detail)

    @staticmethod
    def _response(response, success: bool, message: str):
        response.success = success
        response.message = message
        return response

    def _tick(self) -> None:
        now_ns = self.get_clock().now().nanoseconds
        if self._pending_server:
            if self._action_client.server_is_ready():
                self._send_goal()
            elif time.monotonic() >= self._server_deadline:
                self._pending_server = False
                self._apply(
                    self._progress.goal_failed(
                        'Nav2 action server unavailable'
                    )
                )

        if (
            self._phase_deadline_ns is not None
            and now_ns >= self._phase_deadline_ns
        ):
            command = self._phase_command
            self._clear_phase_deadline()
            callbacks: dict[PatrolCommand, Callable[[], Transition]] = {
                PatrolCommand.START_DWELL:
                    self._progress.dwell_elapsed,
                PatrolCommand.START_RETRY_WAIT:
                    self._progress.retry_wait_elapsed,
                PatrolCommand.START_INTERVAL_WAIT:
                    self._progress.interval_elapsed,
            }
            callback = callbacks.get(command)
            if callback is not None:
                self._apply(callback())

    def _apply(self, transition: Transition) -> None:
        self._detail = transition.message or self._detail
        command = transition.command
        if command == PatrolCommand.SEND_GOAL:
            self._queue_goal()
        elif command == PatrolCommand.CANCEL_GOAL:
            settled = self._invalidate_goal()
            if settled is not None:
                self._detail = settled.message or self._detail
        elif command in {
            PatrolCommand.START_DWELL,
            PatrolCommand.START_INTERVAL_WAIT,
            PatrolCommand.START_RETRY_WAIT,
        }:
            self._set_phase_deadline(command, transition.duration_seconds)
        else:
            self._clear_phase_deadline()
        self.get_logger().info(self._detail)
        self._publish_status()

    def _queue_goal(self) -> None:
        self._clear_phase_deadline()
        if self._request_pending or self._cancelling_tokens:
            self._deferred_send = True
            self._detail = 'waiting for previous Nav2 goal cancellation'
            return
        self._deferred_send = False
        self._goal_token += 1
        self._pending_server = True
        self._server_deadline = time.monotonic() + self._server_timeout
        self._detail = (
            f'waiting for Nav2 to visit '
            f'{self._progress.current_waypoint.name}'
        )

    def _send_goal(self) -> None:
        self._pending_server = False
        waypoint = self._progress.current_waypoint
        token = self._goal_token
        goal = NavigateToPose.Goal()
        pose = PoseStamped()
        pose.header.frame_id = self._route.frame_id
        pose.header.stamp = self.get_clock().now().to_msg()
        pose.pose.position.x = waypoint.x
        pose.pose.position.y = waypoint.y
        quaternion = yaw_to_quaternion(waypoint.yaw)
        pose.pose.orientation.x = quaternion[0]
        pose.pose.orientation.y = quaternion[1]
        pose.pose.orientation.z = quaternion[2]
        pose.pose.orientation.w = quaternion[3]
        goal.pose = pose

        self._request_pending = True
        future = self._action_client.send_goal_async(goal)
        future.add_done_callback(
            lambda completed: self._goal_response(completed, token)
        )
        self._detail = f'sent Nav2 goal: {waypoint.name}'
        self._publish_status()

    def _goal_response(self, future, token: int) -> None:
        self._request_pending = False
        try:
            goal_handle = future.result()
        except Exception as error:  # noqa: B902 - rclpy future boundary
            if self._is_current(token):
                self._apply(
                    self._progress.goal_failed(
                        f'goal request error: {error}'
                    )
                )
            else:
                settled = self._finish_control_without_goal()
                if settled is not None:
                    self._apply(settled)
            return

        if not self._is_current(token):
            if goal_handle.accepted:
                self._cancel_handle(goal_handle, token)
                result_future = goal_handle.get_result_async()
                result_future.add_done_callback(
                    lambda completed: self._goal_result(completed, token)
                )
            else:
                settled = self._finish_control_without_goal()
                if settled is not None:
                    self._apply(settled)
            return
        if not goal_handle.accepted:
            self._apply(self._progress.goal_failed('goal rejected by Nav2'))
            return

        self._active_goal = (token, goal_handle)
        result_future = goal_handle.get_result_async()
        result_future.add_done_callback(
            lambda completed: self._goal_result(completed, token)
        )
        self._detail = (
            f'navigating to {self._progress.current_waypoint.name}'
        )
        self._publish_status()

    def _goal_result(self, future, token: int) -> None:
        if self._active_goal is not None and self._active_goal[0] == token:
            self._active_goal = None
        cancellation_pending = token in self._cancelling_tokens
        try:
            wrapped_result = future.result()
        except Exception as error:  # noqa: B902 - rclpy future boundary
            if cancellation_pending:
                self._detail = (
                    'cannot confirm that the canceled Nav2 goal stopped: '
                    f'{error}'
                )
                self.get_logger().error(self._detail)
                self._publish_status()
                return
            if not self._is_current(token):
                return
            self._apply(
                self._progress.goal_failed(f'goal result error: {error}')
            )
            return

        if cancellation_pending:
            self._settle_cancellation(
                token,
                wrapped_result.status == GoalStatus.STATUS_SUCCEEDED,
            )
            return
        if not self._is_current(token):
            return
        if wrapped_result.status == GoalStatus.STATUS_SUCCEEDED:
            self._apply(self._progress.goal_succeeded())
            return
        if wrapped_result.status == GoalStatus.STATUS_CANCELED:
            self._apply(
                self._progress.goal_canceled(
                    'Nav2 goal was canceled outside patrol control'
                )
            )
            return
        self._apply(
            self._progress.goal_failed(
                f'Nav2 result status {wrapped_result.status}'
            )
        )

    def _is_current(self, token: int) -> bool:
        return (
            token == self._goal_token
            and self._progress.state == PatrolState.NAVIGATING
        )

    def _invalidate_goal(self) -> Transition | None:
        self._goal_token += 1
        self._pending_server = False
        self._deferred_send = False
        self._clear_phase_deadline()
        if self._active_goal is not None:
            token, goal_handle = self._active_goal
            self._active_goal = None
            self._cancel_handle(goal_handle, token)
        elif not self._request_pending:
            return self._finish_control_without_goal()
        return None

    def _cancel_handle(self, goal_handle, token: int) -> None:
        self._cancelling_tokens.add(token)
        try:
            future = goal_handle.cancel_goal_async()
        except Exception as error:  # noqa: B902 - rclpy action boundary
            self._detail = f'Nav2 goal cancellation request failed: {error}'
            self.get_logger().error(self._detail)
            self._publish_status()
            return
        future.add_done_callback(
            lambda completed: self._cancel_response(completed, token)
        )

    def _cancel_response(self, future, token: int) -> None:
        if token not in self._cancelling_tokens:
            return
        try:
            response = future.result()
        except Exception as error:  # noqa: B902 - rclpy action boundary
            self._detail = f'Nav2 goal cancellation response failed: {error}'
            self.get_logger().error(self._detail)
        else:
            if not response.goals_canceling:
                return_code = getattr(response, 'return_code', 'unknown')
                self._detail = (
                    f'Nav2 did not accept cancellation for goal token {token}; '
                    f'return code {return_code}; waiting for its terminal '
                    'result'
                )
                self.get_logger().warning(self._detail)
        self._publish_status()

    def _settle_cancellation(
        self,
        token: int,
        goal_succeeded: bool,
    ) -> None:
        self._cancelling_tokens.discard(token)
        if self._progress.state in {
            PatrolState.PAUSING,
            PatrolState.STOPPING,
        }:
            self._apply(
                self._progress.cancellation_finished(goal_succeeded)
            )
        if (
            not self._cancelling_tokens
            and self._deferred_send
            and not self._request_pending
            and self._progress.state == PatrolState.NAVIGATING
        ):
            self._queue_goal()
        self._publish_status()

    def _finish_control_without_goal(self) -> Transition | None:
        if self._progress.state not in {
            PatrolState.PAUSING,
            PatrolState.STOPPING,
        }:
            return None
        return self._progress.cancellation_finished(False)

    def _set_phase_deadline(
        self,
        command: PatrolCommand,
        duration_seconds: float,
    ) -> None:
        self._phase_command = command
        self._phase_deadline_ns = (
            self.get_clock().now().nanoseconds
            + int(duration_seconds * 1_000_000_000)
        )

    def _clear_phase_deadline(self) -> None:
        self._phase_command = PatrolCommand.NONE
        self._phase_deadline_ns = None

    def _remaining_phase_seconds(self) -> float | None:
        if self._phase_deadline_ns is None:
            return None
        remaining_ns = (
            self._phase_deadline_ns
            - self.get_clock().now().nanoseconds
        )
        return max(0.0, remaining_ns / 1_000_000_000)

    def _publish_status(self) -> None:
        waypoint = self._progress.current_waypoint
        payload = {
            'action_name': self._action_name,
            'cancel_pending': bool(self._cancelling_tokens),
            'current_retries': self._progress.current_retries,
            'detail': self._detail,
            'map_id': self._route.map_id,
            'route': self._route.name,
            'run_cycles_completed':
                self._progress.run_cycles_completed,
            'schedule_mode': self._route.schedule.mode,
            'state': self._progress.state.value,
            'total_cycles_completed':
                self._progress.total_cycles_completed,
            'waypoint_count': len(self._route.waypoints),
            'waypoint_index': self._progress.current_index,
            'waypoint_name': waypoint.name,
        }
        message = String()
        message.data = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
        )
        self._status_publisher.publish(message)


def main(args=None) -> None:
    """Run the patrol manager until ROS shutdown."""
    rclpy.init(args=args)
    node = None
    try:
        node = PatrolManager()
        rclpy.spin(node)
    except KeyboardInterrupt:
        pass
    finally:
        if node is not None:
            node.request_shutdown()
            deadline = time.monotonic() + 1.0
            while (
                rclpy.ok()
                and node.shutdown_pending
                and time.monotonic() < deadline
            ):
                rclpy.spin_once(node, timeout_sec=0.05)
            node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == '__main__':
    main()
